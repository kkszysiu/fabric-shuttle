import copy
import json
import os
import StringIO
import yaml

from fabric.api import cd, put, settings, sudo
from fabric.context_managers import shell_env
from fabric.contrib.files import exists

from .cron import add_crontab_section, CronSchedule, CronJob
from .service import Service
from ..hooks import hook
from ..shared import apt_get_install, pip_install, red, chown

_PACKAGE_URL = 'http://dl.bintray.com/snowplow/snowplow-generic/snowplow_emr_r77_great_auk.zip'
_MASTER_URL = 'https://codeload.github.com/snowplow/snowplow/zip/master'
_MASTER_FILE = 'snowplow-master.zip'

_INSTALL_DIR = '/opt/snowplow'
_MASTER_DIR = os.path.join(_INSTALL_DIR, 'snowplow-master')
_RUNNER_PATH = os.path.join(_INSTALL_DIR, 'snowplow-emr-etl-runner')
_LOADER_PATH = os.path.join(_INSTALL_DIR, 'snowplow-storage-loader')
_CONFIG_PATH = os.path.join(_INSTALL_DIR, 'config.yml')
_RESOLVER_PATH = os.path.join(_INSTALL_DIR, 'iglu_resolver.json')
_CREATE_TABLE_PATH = os.path.join(_MASTER_DIR, '4-storage/postgres-storage/sql/atomic-def.sql')
_ENRICHMENTS_PATH = os.path.join(_MASTER_DIR, '3-enrich/config/enrichments')
_DEFAULT_RESOLVER = {
	'schema': 'iglu:com.snowplowanalytics.iglu/resolver-config/jsonschema/1-0-0',
	'data': {
		'cacheSize': 500,
		'repositories': [
			{
				'name': 'Iglu Central',
				'priority': 0,
				'vendorPrefixes': [ 'com.snowplowanalytics' ],
				'connection': { 'http': { 'uri': 'http://iglucentral.com' } }
			}
		]
	}
}
_CRONTAB_USER = 'root'
_CRONTAB_SECTION = '[snowplow]'
_RUNNER_COMMAND = ' '.join((_RUNNER_PATH, '--config', _CONFIG_PATH, '--resolver', _RESOLVER_PATH))
_LOADER_COMMAND = ' '.join((_LOADER_PATH, '--config', _CONFIG_PATH))

_DEFAULT_SETTINGS = {
	'schedule': CronSchedule(45, 2),
	'chain': None,
	'enrichments': _ENRICHMENTS_PATH,
	'runner_skip': (),
	'loader_skip': ()
}

def _config_postgres(target):
	# Assumes that the target database is already installed, running, and setup with the correct credentials but will try to create both the database and table
	sql = []
	with open(_CREATE_TABLE_PATH) as f:
		for line in f.readlines():
			line = line.strip()
			if line.startswith('--'):
				continue
			sql.append(line.replace('\t', '').replace('\n', '').replace("'", "\\'"))
	sql = ' '.join(sql)

	# Be sure postgressql client is installed and available
	apt_get_install('postgresql-client')

	pg_env = {
		'PGHOST': target['host'],
		'PGPORT': str(target.get('port', '5432')),
		'PGUSER': target['username'],
		'PGPASSWORD': target['password'],
		'PGDATABASE': target['database']
	}
	with shell_env(**pg_env):
		with settings(warn_only=True):
			# Create the database
			sudo('createdb %s' % target['database'])
			# Create the table - currently only atomic.events is supported as the table name
			if target.get('table', 'atomic.events') != 'atomic.events':
				print red('Only atomic.events is supported as a snowplow postgres storage table name.')
				return
			sudo("psql -c $'%s'" % sql)

class Snowplow(Service):
	name = 'snowplow'
	script = None

	def install(self):
		with hook('install %s' % self.name, self):
			if not exists(_INSTALL_DIR):
				apt_get_install('default-jre', 'unzip')
				sudo('mkdir %s' % _INSTALL_DIR)
				with cd(_INSTALL_DIR):
					sudo('wget --no-clobber %s' % _PACKAGE_URL)
					sudo('unzip %s' % _PACKAGE_URL.split('/')[-1])
					sudo('wget --no-clobber %s' % _MASTER_URL)
					sudo('unzip %s' % _MASTER_FILE)

	def config(self):
		# Possible configuration options are custom repositories by setting the repositories setting to an array of repository objects
		with hook('config %s' % self.name, self):
			resolver = copy.deepcopy(_DEFAULT_RESOLVER)
			repositories = self.settings.get('repositories')
			if repositories:
				resolver['data']['repositories'].extend(repositories)
			chown(put(StringIO.StringIO(json.dumps(resolver, indent=4)), _RESOLVER_PATH, use_sudo=True, mode=0644))
			chown(put(self.settings['config_file'], _CONFIG_PATH, use_sudo=True, mode=0644))

			# Read the config file for storage configuration
			with open(self.settings['config_file']) as f:
				config = yaml.load(f)
				if config.get('storage') and config['storage'].get('targets'):
					for target in config['storage']['targets']:
						if target.get('type') == 'postgres':
							_config_postgres(target)

			# Schedule cron jobs
			loader_skip = ','.join(self.settings.get('loader_skip', _DEFAULT_SETTINGS['loader_skip']))
			if loader_skip:
				loader_skip = '--skip ' + loader_skip
			loader_job = CronJob(_LOADER_COMMAND + loader_skip, log_name=self.name, chain=self.settings.get('chain', _DEFAULT_SETTINGS['chain']))
			runner_skip = ','.join(self.settings.get('runner_skip', _DEFAULT_SETTINGS['runner_skip']))
			if runner_skip:
				runner_skip = '--skip ' + runner_skip
			runner_enrichments = self.settings.get('enrichments', _DEFAULT_SETTINGS['enrichments'])
			if runner_enrichments:
				runner_enrichments = '--enrichments ' + runner_enrichments
			runner_job = CronJob(_RUNNER_COMMAND + runner_enrichments + runner_skip, log_name=self.name, schedule=self.settings.get('schedule', _DEFAULT_SETTINGS['schedule']), chain=loader_job)
			remove_crontab_section(_CRONTAB_USER, _CRONTAB_SECTION)
			add_crontab_section(_CRONTAB_USER, _CRONTAB_SECTION, runner_job)
