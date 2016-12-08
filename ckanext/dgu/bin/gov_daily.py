'''
Daily script for gov server
'''
import os
import logging
import sys
import zipfile
import datetime
import re
import urllib2
import json

from common import load_config, register_translator

start_time = datetime.datetime.today()
def report_time_taken(log):
    time_taken = (datetime.datetime.today() - start_time).seconds
    log.info('Time taken: %i seconds' % time_taken)

def get_db_config(config): # copied from fabfile
    url = config['sqlalchemy.url']
    # e.g. 'postgres://tester:pass@localhost/ckantest3'
    db_details_match = re.match('^\s*(?P<db_type>\w*)://(?P<db_user>\w*):?(?P<db_pass>[^@]*)@(?P<db_host>[^/:]*):?(?P<db_port>[^/]*)/(?P<db_name>[\w.-]*)', url)

    db_details = db_details_match.groupdict()
    return db_details


def run_task(taskname):
    return TASKS_TO_RUN and taskname in TASKS_TO_RUN

def command(config_file):
    # Import ckan as it changes the dependent packages imported
    from dump_analysis import (get_run_info, TxtAnalysisFile,
                               CsvAnalysisFile, DumpAnalysisOptions,
                               DumpAnalysis)

    from pylons import config

    # settings
    ckan_instance_name = os.path.basename(config_file).replace('.ini', '')
    if ckan_instance_name not in ['development', 'dgutest']:
        default_dump_dir = '/var/lib/ckan/%s/static/dump' % ckan_instance_name
        default_analysis_dir = '/var/lib/ckan/%s/static/dump_analysis' % ckan_instance_name
        default_backup_dir = '/var/backups/ckan/%s' % ckan_instance_name
        default_openspending_reports_dir = '/var/lib/ckan/%s/openspending_reports' % ckan_instance_name
    else:
        # test purposes
        default_dump_dir = '~/dump'
        default_analysis_dir = '~/dump_analysis'
        default_backup_dir = '~/backups'
        default_openspending_reports_dir = '~/openspending_reports'
    dump_dir = os.path.expanduser(config.get('ckan.dump_dir',
                                             default_dump_dir))
    private_dump_dir = os.path.expanduser(config.get('ckan.private_dump_dir',
                                                     ''))
    analysis_dir = os.path.expanduser(config.get('ckan.dump_analysis_dir',
                                             default_analysis_dir))
    backup_dir = os.path.expanduser(config.get('ckan.backup_dir',
                                               default_backup_dir))
    openspending_reports_dir = os.path.expanduser(config.get('dgu.openspending_reports_dir',
                                                             default_openspending_reports_dir))
    ga_token_filepath = os.path.expanduser(config.get('googleanalytics.token.filepath', ''))
    dump_filebase = config.get('ckan.dump_filename_base',
                               'data.gov.uk-ckan-meta-data-%Y-%m-%d')
    dump_analysis_filebase = config.get('ckan.dump_analysis_base',
                               'data.gov.uk-analysis')
    backup_filebase = config.get('ckan.backup_filename_base',
                                 ckan_instance_name + '.%Y-%m-%d.pg_dump')
    tmp_filepath = config.get('ckan.temp_filepath', '/tmp/dump.tmp')
    openspending_reports_url = config.get('ckan.openspending_reports_url',
                                          'http://data.etl.openspending.org/uk25k/report/')


    log = logging.getLogger('ckanext.dgu.bin.gov_daily')
    log.info('----------------------------')
    log.info('Starting daily script')
    start_time = datetime.datetime.today()

    import ckan.model as model
    import ckan.lib.dumper as dumper
    import ckanext.dgu.lib.dumper as dgu_dumper
    from ckanext.dgu.lib.inventory import unpublished_dumper

    # Check database looks right
    num_packages_before = model.Session.query(model.Package).filter_by(state='active').count()
    log.info('Number of existing active packages: %i' % num_packages_before)
    if num_packages_before < 2:
        log.error('Expected more packages.')
        sys.exit(1)
    elif num_packages_before < 2500:
        log.warn('Expected more packages.')

    # Analytics
    try:
        if ga_token_filepath:
            if run_task('analytics'):
                log.info('Getting analytics for this month')
                from ckanext.ga_report.download_analytics import DownloadAnalytics
                from ckanext.ga_report.ga_auth import (init_service, get_profile_id)
                if not os.path.exists(ga_token_filepath):
                    log.error('GA Token does not exist: %s - not downloading '
                              'analytics' % ga_token_filepath)
                else:
                    try:
                        token, svc = init_service(ga_token_filepath, None)
                    except TypeError, e:
                        log.error('Could not complete authorization for Google '
                                'Analytics. Have you correctly run the '
                                'getauthtoken task and specified the correct '
                                'token file?\nError: %s', e)
                        sys.exit(1)
                    downloader = DownloadAnalytics(svc, token=token, profile_id=get_profile_id(svc),
                                                delete_first=False)
                    downloader.latest()
        else:
            log.info('No token specified, so not downloading Google Analytics data')
    except Exception, exc_analytics:
        log.exception(exc_analytics)
        log.error("Failed to process Google Analytics data (see exception in previous log message)")

    # Copy openspending reports
    if False:  # DISABLED for now  #run_task('openspending'):
        log.info('OpenSpending reports')
        if not os.path.exists(openspending_reports_dir):
            log.info('Creating dump dir: %s' % openspending_reports_dir)
            os.makedirs(openspending_reports_dir)
        try:
            publisher_response = urllib2.urlopen('http://data.gov.uk/api/action/organization_list').read()
        except urllib2.HTTPError, e:
            log.error('Could not get list of publishers for OpenSpending reports: %s',
                      e)
        else:
            try:
                publishers = json.loads(publisher_response)['result']
                assert isinstance(publishers, list), publishers
                assert len(publishers) > 500, len(publishers)
                log.info('Got list of %i publishers starting: %r',
                         len(publishers), publishers[:3])
            except Exception, e:
                log.error('Could not decode list of publishers for OpenSpending reports: %s',
                          e)
            else:
                urls = [openspending_reports_url]
                log.info('Getting reports, starting with: %s', urls[0])
                for publisher in publishers:
                    urls.append('%spublisher-%s.html' % (openspending_reports_url, publisher))

                for url in urls:
                    try:
                        report_response = urllib2.urlopen(url).read()
                    except urllib2.HTTPError, e:
                        if e.code == 404 and url == openspending_reports_url:
                            log.error('Got 404 for openspending report index! %s' % url)
                        elif e.code == 404:
                            log.info('Got 404 for openspending report %s' % url)
                        else:
                            log.error('Could not download openspending report %r: %s',
                                      url, e)
                    else:
                        report_html = report_response
                        # remove header
                        report_html = "".join(report_html.split('---')[2:])
                        # add import timestamp
                        report_html += '<p class="import-date">\n<a href="%s">Page</a> imported from <a href="http://openspending.org/">OpenSpending</a> on %s. Read more about <a href="http://openspending.org/resources/gb-spending/index.html">OpenSpending on data.gov.uk</a>\n</p>' % \
                                       (url.encode('utf8'),
                                        datetime.datetime.now().strftime('%d-%m-%Y'))
                        # add <html>
                        report_html = '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:i18n="http://genshi.edgewall.org/i18n" '\
                                      'xmlns:py="http://genshi.edgewall.org/" xmlns:xi="http://www.w3.org/2001/XInclude" '\
                                      'py:strip="">' + report_html + '</html>'
                        # Sort out non-encoded symbols
                        report_html = re.sub(u' & ', ' &amp; ', report_html)
                        report_html = re.sub('\xc2\xa3', '&pound;', report_html)
                        report_html = re.sub(u'\u2714', '&#x2714;', report_html) # tick
                        report_html = re.sub(u'\u2718', '&#x2718;', report_html) # cross
                        report_html = re.sub(u'\u0141', '&#x0141;', report_html) # pound
                        # save it
                        filename = url[url.rfind('/')+1:] or 'index.html'
                        filepath = os.path.join(openspending_reports_dir, filename)
                        f = open(filepath, 'wb')
                        try:
                            f.write(report_html)
                        finally:
                            f.close()
                        log.info('Wrote openspending report %s', filepath)

    # Create dumps for users
    def create_dump_dir_if_necessary(dump_dir):
        if not os.path.exists(dump_dir):
            log.info('Creating dump dir: %s' % dump_dir)
            os.makedirs(dump_dir)
    if run_task('dump-csv'):
        log.info('Creating database dumps - CSV')
        create_dump_dir_if_necessary(dump_dir)
        dump_file_base = start_time.strftime(dump_filebase)

        logging.getLogger("MARKDOWN").setLevel(logging.WARN)

        # Explicitly dump the packages and resources to their respective CSV
        # files before zipping them up and moving them into position.
        dump_filepath = os.path.join(dump_dir, dump_file_base + '.csv.zip')

        log.info('Creating CSV files: %s' % dump_filepath)
        dumpobj = dgu_dumper.CSVDumper()
        dumpobj.dump()

        dataset_file, resource_file = dumpobj.close()

        log.info('Dumped datasets file is %dMb in size' % (
            os.path.getsize(dataset_file) / (1024 * 1024)))
        log.info('Dumped resources file is %dMb in size' % (
            os.path.getsize(resource_file) / (1024 * 1024)))

        dump_file = zipfile.ZipFile(dump_filepath, 'w', zipfile.ZIP_DEFLATED)
        dump_file.write(dataset_file, "datasets.csv")
        dump_file.write(resource_file, "resources.csv")
        dump_file.close()

        link_filepath = os.path.join(
            dump_dir, 'data.gov.uk-ckan-meta-data-latest.csv.zip')

        if os.path.exists(link_filepath):
            os.unlink(link_filepath)
        os.symlink(dump_filepath, link_filepath)
        os.remove(dataset_file)
        os.remove(resource_file)

    def dump_datasets(file_type, dumper_func, dumper_type, dump_dir,
                      *dumper_args, **dumper_kwargs):
        '''
        Runs the dump, outputing to a tempfile, zips it in correct place,
        adds 'current' symlink.

        dumper_func params depend on dumper_type:
         1: (file object, Package query)
         2: ckanapi.cli.dump.dump_things
        '''
        import gzip
        import shutil
        dump_file_base = start_time.strftime(dump_filebase)
        dump_filename = '%s.%s' % (dump_file_base, file_type)
        zip_filepath = os.path.join(dump_dir, dump_filename + '.zip')
        gz_filepath = os.path.join(dump_dir, dump_filename + '.gz')
        log.info('Creating %s dump: %s', file_type, tmp_filepath)
        # Dump the data to a temporary file
        if dumper_type == 1:
            tmp_file = open(tmp_filepath, 'w+b')
            query = model.Session.query(model.Package) \
                .filter(model.Package.state == 'active')
            dumper_func(tmp_file, query)
            tmp_file.close()
        elif dumper_type == 2:
            dumper_args[2]['--output'] = tmp_filepath
            dumper_func(*dumper_args, **dumper_kwargs)
        try:
            log.info('Dumped data file is %dMB in size' %
                     (os.path.getsize(tmp_filepath) / (1024 * 1024)))
            # Create zip and gz versions
            log.info('Zipping dump to: %s & %s' % (zip_filepath, gz_filepath))
            zip_file = zipfile.ZipFile(zip_filepath, 'w', zipfile.ZIP_DEFLATED)
            zip_file.write(tmp_filepath, dump_filename)
            zip_file.close()
            with open(tmp_filepath, 'rb') as f_in, \
                    gzip.open(gz_filepath, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)

            # Setup a symbolic link to dumps from
            # data.gov.uk-ckan-meta-data-latest.{0}.zip so that it is up-to-date
            # with the latest version for both JSON and CSV.
            for dump_filepath, extension in ((zip_filepath, 'zip'),
                                             (gz_filepath, 'gz')):
                link_filepath = os.path.join(
                    dump_dir,
                    'data.gov.uk-ckan-meta-data-latest.{0}.{1}'.format(
                        file_type, extension))

                if os.path.lexists(link_filepath):
                    os.unlink(link_filepath)
                os.symlink(dump_filepath, link_filepath)
        finally:
            os.remove(tmp_filepath)

    if run_task('dump-csv-unpublished'):
        log.info('Creating database dumps - CSV unpublished')
        create_dump_dir_if_necessary(dump_dir)

        dump_datasets('unpublished.csv', unpublished_dumper, 1, dump_dir)
        report_time_taken(log)

    if run_task('dump-json'):
        log.info('Creating database dumps - JSON')
        create_dump_dir_if_necessary()

        dump_datasets('json', dumper.SimpleDumper().dump_json, 1, dump_dir)
        report_time_taken(log)

    if run_task('dump-json2'):
        # since gov_daily.py is run with sudo, and a path to python in the venv
        # rather than in an activated environment, and ckanapi creates
        # subprocesses, we need to activate the environment. The same one as
        # our current python interpreter.
        bin_dir = os.path.dirname(sys.executable)
        activate_this = os.path.join(bin_dir, 'activate_this.py')
        execfile(activate_this, dict(__file__=activate_this))
        import ckanapi.cli.dump
        log.info('Creating database dumps - JSON 2')
        create_dump_dir_if_necessary(dump_dir)
        ckan = ckanapi.RemoteCKAN('http://localhost', user_agent='daily dump',
                                  get_only=True)
        devnull = open(os.devnull, 'w')
        arguments = ConfigObject()
        arguments['--all'] = True
        #arguments['ID_OR_NAME'] = ['mot-active-vts', 'road-accidents-safety-data']
        arguments['--processes'] = 4
        arguments['--remote'] = config['ckan.site_url']
        arguments['--get-request'] = True
        dump_datasets(
            'v2.jsonl', ckanapi.cli.dump.dump_things, 2, dump_dir,
            ckan, 'datasets', arguments,
            worker_pool=None, stdout=devnull, stderr=devnull)
        report_time_taken(log)

    if run_task('dump-orgs'):
        # since gov_daily.py is run with sudo, and a path to python in the venv
        # rather than in an activated environment, and ckanapi creates
        # subprocesses, we need to activate the environment. The same one as
        # our current python interpreter.
        bin_dir = os.path.dirname(sys.executable)
        activate_this = os.path.join(bin_dir, 'activate_this.py')
        execfile(activate_this, dict(__file__=activate_this))
        import ckanapi.cli.dump
        log.info('Creating database dumps - organization json')
        create_dump_dir_if_necessary(dump_dir)
        ckan = ckanapi.RemoteCKAN('http://localhost', user_agent='daily dump',
                                  get_only=True)
        devnull = open(os.devnull, 'w')
        arguments = ConfigObject()
        arguments['--all'] = True
        arguments['--processes'] = 4
        arguments['--remote'] = config['ckan.site_url']
        arguments['--get-request'] = True
        dump_datasets(
            'organizations.jsonl', ckanapi.cli.dump.dump_things, 2, dump_dir,
            ckan, 'organizations', arguments,
            worker_pool=None, stdout=devnull, stderr=devnull)
        report_time_taken(log)

    if run_task('dump-orgs-private'):
        # since gov_daily.py is run with sudo, and a path to python in the venv
        # rather than in an activated environment, and ckanapi creates
        # subprocesses, we need to activate the environment. The same one as
        # our current python interpreter.
        bin_dir = os.path.dirname(sys.executable)
        activate_this = os.path.join(bin_dir, 'activate_this.py')
        execfile(activate_this, dict(__file__=activate_this))
        import ckanapi.cli.dump
        from ckan import logic
        log.info('Creating database dumps - organizations with private data')
        create_dump_dir_if_necessary(dump_dir)
        ckan = ckanapi.RemoteCKAN('http://localhost', user_agent='daily dump',
                                  get_only=True)
        devnull = open(os.devnull, 'w')
        arguments = ConfigObject()
        arguments['--all'] = True
        arguments['--processes'] = 4
        arguments['--remote'] = config['ckan.site_url']
        arguments['--get-request'] = True
        # apikey is needed to get the (private) user information
        site_user = logic.get_action('get_site_user')({'ignore_auth': True},
            None)
        arguments['--apikey'] = site_user['apikey']
        dump_datasets(
            'organizations.jsonl', ckanapi.cli.dump.dump_things, 2,
            private_dump_dir,
            ckan, 'organizations', arguments,
            worker_pool=None, stdout=devnull, stderr=devnull)
        report_time_taken(log)

    # Dump analysis
    if run_task('dump_analysis'):
        log.info('Doing dump analysis')
        dump_file_base = start_time.strftime(dump_filebase)
        json_dump_filepath = os.path.join(dump_dir, '%s.json.zip' % dump_file_base)
        txt_filepath = os.path.join(analysis_dir, dump_analysis_filebase + '.txt')
        csv_filepath = os.path.join(analysis_dir, dump_analysis_filebase + '.csv')
        log.info('Input: %s', json_dump_filepath)
        log.info('Output: %s & %s', txt_filepath, csv_filepath)
        if not os.path.exists(analysis_dir):
            log.info('Creating dump analysis dir: %s' % analysis_dir)
            os.makedirs(analysis_dir)
        run_info = get_run_info()
        options = DumpAnalysisOptions(analyse_by_source=True)
        analysis = DumpAnalysis(json_dump_filepath, options)
        output_types = (
            # (output_filepath, analysis_file_class)
            (txt_filepath, TxtAnalysisFile),
            (csv_filepath, CsvAnalysisFile),
            )
        for output_filepath, analysis_file_class in output_types:
            log.info('Saving dump analysis to: %s' % output_filepath)
            analysis_file = analysis_file_class(output_filepath, run_info)
            analysis_file.add_analysis(analysis.date, analysis.analysis_dict)
            analysis_file.save()
        report_time_taken(log)

    if run_task('backup'):
        # Create complete backup
        log.info('Creating database backup')
        if not os.path.exists(backup_dir):
            log.info('Creating backup dir: %s' % backup_dir)
            os.makedirs(backup_dir)

        db_details = get_db_config(config)
        pg_dump_filename = start_time.strftime(backup_filebase)
        pg_dump_filepath = os.path.join(backup_dir, pg_dump_filename)
        pg_anon_dump_filepath = os.path.join(
            backup_dir, pg_dump_filename.replace('.pg_dump', '.anon_pg_dump.gz'))
        cmd = 'export PGPASSWORD=%(db_pass)s&&pg_dump ' % db_details
        for pg_dump_option, db_details_key in (('U', 'db_user'),
                                               ('h', 'db_host'),
                                               ('p', 'db_port')):
            if db_details.get(db_details_key):
                cmd += '-%s %s ' % (pg_dump_option, db_details[db_details_key])
        cmd += ' -E utf8 %(db_name)s' % db_details + ' > %s' % pg_dump_filepath
        log.info('Backup command: %s' % cmd)
        ret = os.system(cmd)
        if ret == 0:
            log.info('Backup successful: %s' % pg_dump_filepath)
            from anonymize_sql import anonymize_files
            log.info('Anonymizing to: %s' % pg_anon_dump_filepath)
            num_users = anonymize_files(pg_dump_filepath,
                                        pg_anon_dump_filepath)
            if num_users < 500:
                log.error('Not enough users anonymized in backup - %i users. '
                          'Not anonymized successfully so deleting the file',
                          num_users)
                os.remove(pg_anon_dump_filepath)
            else:
                log.info('Created anonymous backup: %s (%i users)',
                        pg_anon_dump_filepath, num_users)
            log.info('Zipping up backup')
            pg_dump_zipped_filepath = pg_dump_filepath + '.gz'
            # -f to overwrite any existing file, instead of prompt Yes/No
            cmd = 'gzip -f %s' % pg_dump_filepath
            log.info('Zip command: %s' % cmd)
            ret = os.system(cmd)
            if ret == 0:
                log.info('Backup gzip successful: %s' % pg_dump_zipped_filepath)
            else:
                log.error('Backup gzip error: %s' % ret)
            # Only give read permission to anon backup, unless root, to
            # encourage use of the anonymous versions
            cmd = 'chmod 640 %s' % pg_dump_zipped_filepath
            log.info('Chmod command: %s' % cmd)
            ret = os.system(cmd)
            if ret == 0:
                log.info('Backup chmod successful: %s' % pg_dump_zipped_filepath)
            else:
                log.error('Backup chmod error: %s' % ret)
        else:
            log.error('Backup error: %s' % ret)

    # Log footer
    report_time_taken(log)
    log.info('Finished daily script')
    log.info('----------------------------')


class ConfigObject(dict):
    '''A dict which doesn't barf when you've not set an option'''
    def __getitem__(self, key):
        if key in self:
            return dict.__getitem__(self, key)
        return  # not KeyError


TASKS_TO_RUN = ['analytics', 'openspending',
                'dump-csv', 'dump-csv-unpublished', 'dump-json', 'dump-json2',
                'dump-orgs', 'dump-orgs-private',
                'dump_analysis', 'backup']

if __name__ == '__main__':
    USAGE = '''Daily script for government
    Usage: python %s <config.ini> [task]

    Where:
       [task] - task to run (optional), picked from:
                %s
                or run multiple by separating by a comma.
    ''' % (sys.argv[0], ','.join(TASKS_TO_RUN))

    if set(sys.argv) & set(('--help', '-h')):
        print USAGE
        sys.exit(1)
    if len(sys.argv) < 2:
        err = 'Error: Please specify config file.'
        print USAGE, err
        logging.error('%s' % err)
        sys.exit(1)
    config_file = sys.argv[1]
    config_ini_filepath = os.path.abspath(config_file)

    if len(sys.argv) == 3:
        TASKS_TO_RUN = sys.argv[2].split(',')

    load_config(config_ini_filepath)
    register_translator()
    logging.config.fileConfig(config_ini_filepath)

    command(config_file)
