
__copyright__ = "Copyright 2013-2016, http://radical.rutgers.edu"
__license__   = "MIT"

import os
import sys
import copy
import glob
import threading

import radical.utils                as ru
import radical.saga                 as rs
import radical.saga.utils.pty_shell as rsup


from .resource_config import ResourceConfig
from .db              import DBSession
from .utils           import version_detail as rp_version_detail
from .                import utils          as rpu


# ------------------------------------------------------------------------------
#
class Session(rs.Session):
    '''
    A Session is the root object of all RP objects in an application instance:
    it holds :class:`radical.pilot.PilotManager` and
    :class:`radical.pilot.UnitManager` instances which in turn hold
    :class:`radical.pilot.ComputePilot` and :class:`radical.pilot.ComputeUnit`
    instances, and several other components which operate on those stateful
    entities.
    '''

    # In that role, the session will create a special pubsub channel `heartbeat`
    # which is used by all components in its hierarchy to exchange heartbeat
    # messages.  Those messages are used to watch component health - if
    # a (parent or child) component fails to send heartbeats for a certain
    # amount of time, it is considered dead and the process tree will terminate.
    # That heartbeat management is implemented in the `ru.Heartbeat` class.
    # Only primary sessions instantiate a heartbeat channel (i.e., only the root
    # sessions of RP client or agent modules), but all components need to call
    # the sessions `heartbeat()` method at regular intervals.

    # the reporter is an applicataion-level singleton
    _reporter = None

    # --------------------------------------------------------------------------
    #
    def __init__(self, dburl=None, uid=None, cfg=None, _primary=True):
        '''
        Creates a new session.  A new Session instance is created and
        stored in the database.

        **Arguments:**
            * **dburl** (`string`): The MongoDB URL.  If none is given,
              RP uses the environment variable RADICAL_PILOT_DBURL.  If that is
              not set, an error will be raises.

            * **uid** (`string`): Create a session with this UID.  Session UIDs
              MUST be unique - otherwise they will lead to conflicts in the
              underlying database, resulting in undefined behaviours (or worse).

            * **_primary** (`bool`): only sessions created by the original
              application process (via `rp.Session()`, will connect to the  DB.
              Secondary session instances are instantiated internally in
              processes spawned (directly or indirectly) by the initial session,
              for example in some of it's components.  A secondary session will
              inherit the original session ID, but will not attempt to create
              a new DB collection - if such a DB connection is needed, the
              component needs to establish that on its own.  '''

        # class state
        self._dbs         = None
        self._uid         = None
        self._closed      = False

        self._pmgrs       = dict()  # map IDs to pmgr instances
        self._umgrs       = dict()  # map IDs to umgr instances
        self._bridges     = list()  # list of bridge    IDs for hb monitoring
        self._components  = list()  # list of compponen IDs for hb monitoring

        # The resource configuration dictionary associated with the session.
        self._resource_configs = {}

        # if a config is given, use it
        if cfg:
            self._cfg = copy.deepcopy(cfg)

        else:
            # otherwise we need to load the config
            cfg_name  = os.environ.get('RADICAL_PILOT_SESSION_CFG', 'default')
            self._cfg = ru.Config("configs/session_%s.json" % cfg_name)


        # cache sandboxes etc.
        if 'client_sandbox' in self._cfg:
            self._client_sandbox = self._cfg['client_sandbox']
        else:
            self._client_sandbox = os.getcwd()

        self._cache_lock  = threading.RLock()
        self._cache       = {'resource_sandbox' : dict(),
                             'session_sandbox'  : dict(),
                             'pilot_sandbox'    : dict()}

        # fall back to config data where possible
        # sanity check on parameters
        if not uid :
            uid = self._cfg.get('session_id')

        if uid:
            self._uid = uid

        else:
            if not _primary:
                raise ValueError('non-primary sessions need a session UID')

            # primary sessions can generate new uids
            self._uid = ru.generate_id('rp.session',  mode=ru.ID_PRIVATE)

        if not self._cfg.get('session_id'): self._cfg['session_id'] = self._uid
        if not self._cfg.get('pwd')       : self._cfg['pwd']        = '%s/%s' \
                                                     % (os.getcwd(), self._uid)
        self._cfg['owner'] = None

        self._pwd  = self._cfg['pwd']
        self._prof = self._get_profiler(name=self._cfg['owner'])
        self._rep  = self._get_reporter(name=self._cfg['owner'])
        self._log  = self._get_logger  (name=self._cfg['owner'],
                                       level=self._cfg.get('debug'))

        # now we have config and uid - initialize base class (saga session)
        self._prof.prof('session_start', uid=self._uid)
        self._rep.info ('<<new session: ')
        self._rep.plain('[%s]' % self._uid)
        rs.Session.__init__(self, uid=self._uid)

        self._load_resource_configs()


        # --------------------------------------------------------------------
        # create db connection for primary sessions
        if _primary:

            # we need a dburl to connect to.
            if not dburl:
                dburl = os.environ.get("RADICAL_PILOT_DBURL")

            if not dburl:
                dburl = self._cfg.get('default_dburl')

            if not dburl:
                dburl = self._cfg.get('dburl')

            if not dburl:
                raise RuntimeError("no database URL (set RADICAL_PILOT_DBURL)")


            dburl = ru.Url(dburl)
            # if the database url contains a path element, we interpret that as
            # database name (without the leading slash)
            if  not dburl.path         or \
                dburl.path[0]   != '/' or \
                len(dburl.path) <=  1  :
                if not uid:
                    # we fake reconnnect if no DB is available -- but otherwise we
                    # really really need a db connection...
                    raise ValueError("incomplete DBURL '%s' no db name!" % dburl)

            self._log.info("using database %s" % dburl)
            self._rep.info ('<<database   : ')
            self._rep.plain('[%s]' % dburl)

            # create/connect database handle on primary sessions
            try:
                self._dbs = DBSession(sid=self.uid, dburl=str(dburl),
                                      cfg=self._cfg, logger=self._log)

                # from here on we should be able to close the session again
                self._log.info("New Session created: %s." % self.uid)

                self._cfg['dburl'] = str(dburl)

                py_version_detail = sys.version.replace("\n", " ")
                self.inject_metadata({'radical_stack':
                                             {'rp': rp_version_detail,
                                              'rs': rs.version_detail,
                                              'ru': ru.version_detail,
                                              'py': py_version_detail}})
            except Exception as e:
                self._rep.error(">>err\n")
                self._log.exception('session create failed [%s]', dburl)
                raise RuntimeError('session create failed [%s]: %s'
                                  % (dburl, e))


        self._rec = os.environ.get('RADICAL_PILOT_RECORD_SESSION')
        if self._primary and self._rec:

            # append session ID to recording path
            self._rec = "%s/%s" % (self._rec, self._uid)

            # create recording path and record session
            os.system('mkdir -p %s' % self._rec)
            ru.write_json({'dburl': str(self.dburl)},
                          "%s/session.json" % self._rec)
            self._log.info("recording session in %s" % self._rec)


        # if bridges and components are specified in the config, start them
        ruc = rpu.Component
        self._bridges    = ruc.start_bridges   (self._cfg, self, self._log)
        self._components = ruc.start_components(self._cfg, self, self._log)

        # at this point we have a DB connection, logger, etc, and are done
        self._log.info('radical.pilot version: %s' % rp_version_detail)
        self._log.info('radical.saga  version: %s' % rs.version_detail)
        self._log.info('radical.utils version: %s' % ru.version_detail)

        self._rep.ok('>>ok\n')


    # --------------------------------------------------------------------------
    # Allow Session to function as a context manager in a `with` clause
    def __enter__(self):
        return self


    # --------------------------------------------------------------------------
    # Allow Session to function as a context manager in a `with` clause
    def __exit__(self, type, value, traceback):

        # FIXME: use cleanup_on_close, terminate_on_close attributes
        self.close()


    # --------------------------------------------------------------------------
    #
    def _load_resource_configs(self):

        self._prof.prof('config_parser_start', uid=self._uid)

        # Loading all "default" resource configurations
        module_path  = os.path.dirname(os.path.abspath(__file__))
        default_cfgs = "%s/configs/resource_*.json" % module_path
        config_files = glob.glob(default_cfgs)

        for config_file in config_files:

            try:
                self._log.info("Load resource configurations from %s" % config_file)
                rcs = ResourceConfig.from_file(config_file)
            except Exception as e:
                self._log.exception("skip config file %s: %s" % (config_file, e))
                raise RuntimeError('config error (%s) - abort' % e)

            for rc in rcs:
                self._log.info("Load resource configurations for %s" % rc)
                self._resource_configs[rc] = rcs[rc].as_dict()
                self._log.debug('read rcfg for %s (%s)',
                        rc, self._resource_configs[rc].get('cores_per_node'))

        home         = os.environ.get('HOME', '')
        user_cfgs    = "%s/.radical/pilot/configs/resource_*.json" % home
        config_files = glob.glob(user_cfgs)

        for config_file in config_files:

            try:
                rcs = ResourceConfig.from_file(config_file)
            except Exception as e:
                self._log.exception("skip config file %s: %s" % (config_file, e))
                raise RuntimeError('config error (%s) - abort' % e)

            for rc in rcs:
                self._log.info("Load resource configurations for %s" % rc)

                if rc in self._resource_configs:
                    # config exists -- merge user config into it
                    ru.dict_merge(self._resource_configs[rc],
                                  rcs[rc].as_dict(),
                                  policy='overwrite')
                else:
                    # new config -- add as is
                    self._resource_configs[rc] = rcs[rc].as_dict()

                self._log.debug('fix  rcfg for %s (%s)',
                        rc, self._resource_configs[rc].get('cores_per_node'))

        default_aliases = "%s/configs/resource_aliases.json" % module_path
        self._resource_aliases = ru.read_json_str(default_aliases)['aliases']

        # check if we have aliases to merge
        usr_aliases = '%s/.radical/pilot/configs/resource_aliases.json' % home
        if os.path.isfile(usr_aliases):
            ru.dict_merge(self._resource_aliases,
                          ru.read_json_str(usr_aliases).get('aliases', {}),
                          policy='overwrite')

        self._prof.prof('config_parser_stop', uid=self._uid)


    # --------------------------------------------------------------------------
    #
    def close(self, cleanup=False, terminate=True, download=False):
        '''Closes the session.

        All subsequent attempts access objects attached to the session will
        result in an error. If cleanup is set to True (default) the session
        data is removed from the database.

        **Arguments:**
            * **cleanup** (`bool`): Remove session from MongoDB (implies * terminate)
            * **terminate** (`bool`): Shut down all pilots associated with the session.

        **Raises:**
            * :class:`radical.pilot.IncorrectState` if the session is closed
              or doesn't exist.
        '''

        # close only once
        if self._closed:
            return

        self._rep.info('closing session %s' % self._uid)
        self._log.debug("session %s closing", self._uid)
        self._prof.prof("session_close", uid=self._uid)

        # set defaults
        if cleanup   is None: cleanup   = True
        if terminate is None: terminate = True

        if  cleanup:
            # cleanup implies terminate
            terminate = True

        for umgr_uid,umgr in self._umgrs.items():
            self._log.debug("session %s closes umgr   %s", self._uid, umgr_uid)
            umgr.close()
            self._log.debug("session %s closed umgr   %s", self._uid, umgr_uid)

        for pmgr_uid,pmgr in self._pmgrs.items():
            self._log.debug("session %s closes pmgr   %s", self._uid, pmgr_uid)
            pmgr.close(terminate=terminate)
            self._log.debug("session %s closed pmgr   %s", self._uid, pmgr_uid)

        for comp in self._components:
            self._log.debug("session %s closes comp   %s", self._uid, comp.uid)
            comp.stop()
            comp.join()
            self._log.debug("session %s closed comp   %s", self._uid, comp.uid)

        for bridge in self._bridges:
            self._log.debug("session %s closes bridge %s", self._uid, bridge.uid)
            bridge.stop()
            bridge.join()
            self._log.debug("session %s closed bridge %s", self._uid, bridge.uid)

        if self._dbs:
            self._log.debug("session %s closes db (%s)", self._uid, cleanup)
            self._dbs.close(delete=cleanup)

        self._log.debug("session %s closed (delete=%s)", self._uid, cleanup)
        self._prof.prof("session_stop", uid=self._uid)
        self._prof.close()

        # support GC
        for x in self._to_close:
            try:    x.close()
            except: pass
        for x in self._to_stop:
            try:    x.stop()
            except: pass
        for x in self._to_destroy:
            try:    x.destroy()
            except: pass

        self._closed = True

        # after all is said and done, we attempt to download the pilot log- and
        # profiles, if so wanted
        if download:

            self._prof.prof("session_fetch_start", uid=self._uid)
            self._log.debug('start download')
            tgt = os.getcwd()
            self.fetch_json    (tgt='%s/%s' % (tgt, self.uid))
            self.fetch_profiles(tgt=tgt)
            self.fetch_logfiles(tgt=tgt)

            self._prof.prof("session_fetch_stop", uid=self._uid)

        self._rep.info('<<session lifetime: %.1fs' % (self.closed - self.created))
        self._rep.ok('>>ok\n')


    # --------------------------------------------------------------------------
    #
    def as_dict(self):
        '''Returns a Python dictionary representation of the object.
        '''

        object_dict = {
            "uid"       : self._uid,
            "created"   : self.created,
            "connected" : self.connected,
            "closed"    : self.closed,
            "dburl"     : str(self.dburl),
            "cfg"       : copy.deepcopy(self._cfg)
        }
        return object_dict


    # --------------------------------------------------------------------------
    #
    def __str__(self):
        '''Returns a string representation of the object.
        '''
        return str(self.as_dict())


    # --------------------------------------------------------------------------
    #
    @property
    def uid(self):
        return self._uid


    # --------------------------------------------------------------------------
    #
    @property
    def logdir(self):
        return self._cfg['pwd']


    # --------------------------------------------------------------------------
    #
    @property
    def dburl(self):
        return self._cfg.get('dburl')


    # --------------------------------------------------------------------------
    #
    def get_db(self):

        if self._dbs: return self._dbs.get_db()
        else        : return None


    # --------------------------------------------------------------------------
    #
    @property
    def created(self):
        '''Returns the UTC date and time the session was created.
        '''
        if self._dbs: return self._dbs.created
        else        : return None


    # --------------------------------------------------------------------------
    #
    @property
    def connected(self):
        '''
        Return time when the session connected to the DB
        '''

        if self._dbs: return self._dbs.connected
        else        : return None


    # -------------------------------------------------------------------------
    #
    @property
    def is_connected(self):

        return self._dbs.is_connected


    # --------------------------------------------------------------------------
    #
    @property
    def closed(self):
        '''
        Returns the time of closing
        '''
        if self._dbs: return self._dbs.closed
        else        : return None


    # --------------------------------------------------------------------------
    #
    def _get_logger(self, name, level=None):
        '''
        This is a thin wrapper around `ru.Logger()` which makes sure that
        log files end up in a separate directory with the name of `session.uid`.
        '''
        return ru.Logger(name=name, ns='radical.pilot', targets=['.'],
                         path=self._pwd, level=level)


    # --------------------------------------------------------------------------
    #
    def _get_reporter(self, name):
        '''
        This is a thin wrapper around `ru.Reporter()` which makes sure that
        log files end up in a separate directory with the name of `session.uid`.
        '''

        if not self._reporter:
            self._reporter = ru.Reporter(name=name, ns='radical.pilot',
                                         targets=['stdout'], path=self._pwd)
        return self._reporter


    # --------------------------------------------------------------------------
    #
    def _get_profiler(self, name):
        '''
        This is a thin wrapper around `ru.Profiler()` which makes sure that
        log files end up in a separate directory with the name of `session.uid`.
        '''

        prof = ru.Profiler(name=name, ns='radical.pilot', path=self._pwd)

        return prof


    # --------------------------------------------------------------------------
    #
    def inject_metadata(self, metadata):
        '''
        Insert (experiment) metadata into an active session
        RP stack version info always get added.
        '''

        if not isinstance(metadata, dict):
            raise Exception("Session metadata should be a dict!")

        if self._dbs and self._dbs._c:
            self._dbs._c.update({'type'  : 'session',
                                 "uid"   : self.uid},
                                {"$push" : {"metadata": metadata}})


    # --------------------------------------------------------------------------
    #
    def _register_pmgr(self, pmgr):

        self._dbs.insert_pmgr(pmgr.as_dict())
        self._pmgrs[pmgr.uid] = pmgr


    # --------------------------------------------------------------------------
    #
    def list_pilot_managers(self):
        '''
        Lists the unique identifiers of all :class:`radical.pilot.PilotManager`
        instances associated with this session.

        **Returns:**
            * A list of :class:`radical.pilot.PilotManager` uids
              (`list` of `strings`).
        '''

        return list(self._pmgrs.keys())


    # --------------------------------------------------------------------------
    #
    def get_pilot_managers(self, pmgr_uids=None):
        '''
        returns known PilotManager(s).

        **Arguments:**

            * **pmgr_uids** [`string`]:
              unique identifier of the PilotManager we want

        **Returns:**
            * One or more [:class:`radical.pilot.PilotManager`] objects.
        '''

        return_scalar = False
        if not isinstance(pmgr_uids, list):
            pmgr_uids     = [pmgr_uids]
            return_scalar = True

        if pmgr_uids: pmgrs = [self._pmgrs[uid] for uid in pmgr_uids]
        else        : pmgrs =  list(self._pmgrs.values())

        if return_scalar: return pmgrs[0]
        else            : return pmgrs


    # --------------------------------------------------------------------------
    #
    def _register_umgr(self, umgr):

        self._dbs.insert_umgr(umgr.as_dict())
        self._umgrs[umgr.uid] = umgr


    # --------------------------------------------------------------------------
    #
    def list_unit_managers(self):
        '''
        Lists the unique identifiers of all :class:`radical.pilot.UnitManager`
        instances associated with this session.

        **Returns:**
            * A list of :class:`radical.pilot.UnitManager` uids (`list` of `strings`).
        '''

        return list(self._umgrs.keys())


    # --------------------------------------------------------------------------
    #
    def get_unit_managers(self, umgr_uids=None):
        '''
        returns known UnitManager(s).

        **Arguments:**

            * **umgr_uids** [`string`]:
              unique identifier of the UnitManager we want

        **Returns:**
            * One or more [:class:`radical.pilot.UnitManager`] objects.
        '''

        return_scalar = False
        if not isinstance(umgr_uids, list):
            umgr_uids     = [umgr_uids]
            return_scalar = True

        if umgr_uids: umgrs = [self._umgrs[uid] for uid in umgr_uids]
        else        : umgrs =  list(self._umgrs.values())

        if return_scalar: return umgrs[0]
        else            : return umgrs


    # -------------------------------------------------------------------------
    #
    def list_resources(self):
        '''
        Returns a list of known resource labels which can be used in a pilot
        description.  Not that resource aliases won't be listed.
        '''

        return sorted(self._resource_configs.keys())


    # -------------------------------------------------------------------------
    #
    def add_resource_config(self, resource_config):
        '''Adds a new :class:`radical.pilot.ResourceConfig` to the PilotManager's
           dictionary of known resources, or accept a string which points to
           a configuration file.

           For example::

                  rc = radical.pilot.ResourceConfig(label="mycluster")
                  rc.job_manager_endpoint = "ssh+pbs://mycluster
                  rc.filesystem_endpoint  = "sftp://mycluster
                  rc.default_queue        = "private"
                  rc.bootstrapper         = "default_bootstrapper.sh"

                  pm = radical.pilot.PilotManager(session=s)
                  pm.add_resource_config(rc)

                  pd = radical.pilot.ComputePilotDescription()
                  pd.resource = "mycluster"
                  pd.cores    = 16
                  pd.runtime  = 5 # minutes

                  pilot = pm.submit_pilots(pd)
        '''

        if isinstance(resource_config, str):

            # let exceptions fall through
            rcs = ResourceConfig.from_file(resource_config)

            for rc in rcs:
                self._log.info('load rcfg for %s' % rc)
                self._resource_configs[rc] = rcs[rc].as_dict()

        else:
            self._log.debug('load rcfg for %s', resource_config.label)
            self._resource_configs[resource_config.label] = resource_config.as_dict()


    # -------------------------------------------------------------------------
    #
    def get_resource_config(self, resource, schema=None):
        '''
        Returns a dictionary of the requested resource config
        '''

        if  resource in self._resource_aliases:
            self._log.warning("using alias '%s' for deprecated resource '%s'"
                              % (self._resource_aliases[resource], resource))
            resource = self._resource_aliases[resource]

        if  resource not in self._resource_configs:
            raise RuntimeError("Resource '%s' is not known." % resource)

        resource_cfg = copy.deepcopy(self._resource_configs[resource])

        if  not schema:
            if 'schemas' in resource_cfg:
                schema = resource_cfg['schemas'][0]

        if  schema:
            if  schema not in resource_cfg:
                raise RuntimeError("schema %s unknown for resource %s"
                                  % (schema, resource))

            for key in resource_cfg[schema]:
                # merge schema specific resource keys into the
                # resource config
                resource_cfg[key] = resource_cfg[schema][key]

        return resource_cfg


    # -------------------------------------------------------------------------
    #
    def fetch_profiles(self, tgt=None, fetch_client=False):

        return rpu.fetch_profiles(self._uid, dburl=self.dburl, tgt=tgt,
                                  session=self)


    # -------------------------------------------------------------------------
    #
    def fetch_logfiles(self, tgt=None, fetch_client=False):

        return rpu.fetch_logfiles(self._uid, dburl=self.dburl, tgt=tgt,
                                  session=self)


    # -------------------------------------------------------------------------
    #
    def fetch_json(self, tgt=None, fetch_client=False):

        return rpu.fetch_json(self._uid, dburl=self.dburl, tgt=tgt,
                              session=self)



    # -------------------------------------------------------------------------
    #
    def _get_client_sandbox(self):
        '''
        For the session in the client application, this is os.getcwd().  For the
        session in any other component, specifically in pilot components, the
        client sandbox needs to be read from the session config (or pilot
        config).  The latter is not yet implemented, so the pilot can not yet
        interpret client sandboxes.  Since pilot-side stagting to and from the
        client sandbox is not yet supported anyway, this seems acceptable
        (FIXME).
        '''

        return self._client_sandbox


    # -------------------------------------------------------------------------
    #
    def _get_resource_sandbox(self, pilot):
        '''
        for a given pilot dict, determine the global RP sandbox, based on the
        pilot's 'resource' attribute.
        '''

        # FIXME: this should get 'resource, schema=None' as parameters

        resource = pilot['description'].get('resource')
        schema   = pilot['description'].get('access_schema')

        if not resource:
            raise ValueError('Cannot get pilot sandbox w/o resource target')

        # the global sandbox will be the same for all pilots on any resource, so
        # we cache it
        with self._cache_lock:

            if resource not in self._cache['resource_sandbox']:

                # cache miss -- determine sandbox and fill cache
                rcfg   = self.get_resource_config(resource, schema)
                fs_url = rs.Url(rcfg['filesystem_endpoint'])

                # Get the sandbox from either the pilot_desc or resource conf
                sandbox_raw = pilot['description'].get('sandbox')
                if not sandbox_raw:
                    sandbox_raw = rcfg.get('default_remote_workdir', "$PWD")


                # we may need to replace pat elements with data from the pilot
                # description
                if '%' in sandbox_raw:
                    # expand from pilot description
                    expand = dict()
                    for k,v in pilot['description'].items():
                        if v is None:
                            v = ''
                        expand['pd.%s' % k] = v
                        if isinstance(v, str):
                            expand['pd.%s' % k.upper()] = v.upper()
                            expand['pd.%s' % k.lower()] = v.lower()
                        else:
                            expand['pd.%s' % k.upper()] = v
                            expand['pd.%s' % k.lower()] = v
                    sandbox_raw = sandbox_raw % expand


                # If the sandbox contains expandables, we need to resolve those
                # remotely.
                #
                # NOTE: this will only work for (gsi)ssh or similar shell
                #       based access mechanisms
                if '$' not in sandbox_raw:
                    # no need to expand further
                    sandbox_base = sandbox_raw

                else:
                    js_url = rcfg['job_manager_endpoint']
                    js_url = rcfg.get('job_manager_hop', js_url)
                    js_url = rs.Url(js_url)

                    elems  = js_url.schema.split('+')

                    if   'ssh'    in elems: js_url.schema = 'ssh'
                    elif 'gsissh' in elems: js_url.schema = 'gsissh'
                    elif 'fork'   in elems: js_url.schema = 'fork'
                    elif len(elems) == 1  : js_url.schema = 'fork'
                    else: raise Exception("invalid schema: %s" % js_url.schema)

                    if js_url.schema == 'fork':
                        js_url.hostname = 'localhost'

                    self._log.debug("rsup.PTYShell('%s')", js_url)
                    shell = rsup.PTYShell(js_url, self)

                    ret, out, err = shell.run_sync(' echo "WORKDIR: %s"'
                                                                  % sandbox_raw)
                    if ret or 'WORKDIR:' not in out:
                        raise RuntimeError("Couldn't get remote workdir.")

                    sandbox_base = out.split(":")[1].strip()
                    self._log.debug("sandbox base %s: %s", js_url, sandbox_base)

                # at this point we have determined the remote 'pwd' - the
                # global sandbox is relative to it.
                fs_url.path = "%s/radical.pilot.sandbox" % sandbox_base

                # before returning, keep the URL string in cache
                self._cache['resource_sandbox'][resource] = fs_url

            return self._cache['resource_sandbox'][resource]


    # --------------------------------------------------------------------------
    #
    def _get_session_sandbox(self, pilot):

        # FIXME: this should get 'resource, schema=None' as parameters

        resource = pilot['description'].get('resource')

        if not resource:
            raise ValueError('Cannot get session sandbox w/o resource target')

        with self._cache_lock:

            if resource not in self._cache['session_sandbox']:

                # cache miss
                resource_sandbox      = self._get_resource_sandbox(pilot)
                session_sandbox       = rs.Url(resource_sandbox)
                session_sandbox.path += '/%s' % self.uid

                self._cache['session_sandbox'][resource] = session_sandbox

            return self._cache['session_sandbox'][resource]


    # --------------------------------------------------------------------------
    #
    def _get_pilot_sandbox(self, pilot):

        # FIXME: this should get 'pid, resource, schema=None' as parameters

        pilot_sandbox = pilot.get('pilot_sandbox')
        if str(pilot_sandbox):
            return rs.Url(pilot_sandbox)

        pid = pilot['uid']
        with self._cache_lock:
            if  pid in self._cache['pilot_sandbox']:
                return self._cache['pilot_sandbox'][pid]

        # cache miss
        session_sandbox     = self._get_session_sandbox(pilot)
        pilot_sandbox       = rs.Url(session_sandbox)
        pilot_sandbox.path += '/%s/' % pilot['uid']

        with self._cache_lock:
            self._cache['pilot_sandbox'][pid] = pilot_sandbox

        return pilot_sandbox


    # --------------------------------------------------------------------------
    #
    def _get_unit_sandbox(self, unit, pilot):

        # If a sandbox is specified in the unit description, then interpret
        # relative paths as relativet to the pilot sandbox.

        # unit sandboxes are cached in the unit dict
        unit_sandbox = unit.get('unit_sandbox')
        if unit_sandbox:
            return unit_sandbox

        # specified in description?
        if not unit_sandbox:
            sandbox  = unit['description'].get('sandbox')
            if sandbox:
                unit_sandbox = ru.Url(self._get_pilot_sandbox(pilot))
                if sandbox[0] == '/':
                    unit_sandbox.path = unit_sandbox
                else:
                    unit_sandbox.path += '/%s/' % sandbox

        # default
        if not unit_sandbox:
            unit_sandbox = ru.Url(self._get_pilot_sandbox(pilot))
            unit_sandbox.path += "/%s/" % unit['uid']

        # cache
        unit['unit_sandbox'] = str(unit_sandbox)

        return unit_sandbox


    # --------------------------------------------------------------------------
    #
    def _get_jsurl(self, pilot):
        '''
        get job service endpoint and hop URL for the pilot's target resource.
        '''

        resrc   = pilot['description']['resource']
        schema  = pilot['description']['access_schema']
        rcfg    = self.get_resource_config(resrc, schema)

        js_url  = rs.Url(rcfg.get('job_manager_endpoint'))
        js_hop  = rs.Url(rcfg.get('job_manager_hop', js_url))

        # make sure the js_hop url points to an interactive access
        # TODO: this is an unreliable heuristics - we should require the js_hop
        #       URL to be specified in the resource configs.
        if   '+gsissh' in js_hop.schema or \
             'gsissh+' in js_hop.schema    : js_hop.schema = 'gsissh'
        elif '+ssh'    in js_hop.schema or \
             'ssh+'    in js_hop.schema    : js_hop.schema = 'ssh'
        else                               : js_hop.schema = 'fork'

        return js_url, js_hop


    # --------------------------------------------------------------------------
    #
    @staticmethod
    def autopilot(user, passwd):

        import github3
        import random

        labels = 'type:autopilot'
        titles = ['+++ Out of Cheese Error +++',
                  '+++ Redo From Start! +++',
                  '+++ Mr. Jelly! Mr. Jelly! +++',
                  '+++ Melon melon melon',
                  '+++ Wahhhhhhh! Mine! +++',
                  '+++ Divide By Cucumber Error +++',
                  '+++ Please Reinstall Universe And Reboot +++',
                  '+++ Whoops! Here comes the cheese! +++',
                  '+++ End of Cheese Error +++',
                  '+++ Can Not Find Drive Z: +++',
                  '+++ Unknown Application Error +++',
                  '+++ Please Reboot Universe +++',
                  '+++ Year Of The Sloth +++',
                  '+++ error of type 5307 has occured +++',
                  '+++ Eternal domain error +++',
                  '+++ Error at Address Number 6, Treacle Mine Road +++']

        def excuse():
            cmd_fetch  = "telnet bofh.jeffballard.us 666 2>&1 "
            cmd_filter = "grep 'Your excuse is:' | cut -f 2- -d :"
            out        = ru.sh_callout("%s | %s" % (cmd_fetch, cmd_filter),
                                       shell=True)[0]
            return out.strip()


        github = github3.login(user, passwd)
        repo   = github.repository("radical-cybertools", "radical.pilot")

        title = 'autopilot: %s' % titles[random.randint(0, len(titles) - 1)]

        print('----------------------------------------------------')
        print('autopilot')

        for issue in repo.issues(labels=labels, state='open'):
            if issue.title == title:
                reply = 'excuse: %s' % excuse()
                issue.create_comment(reply)
                print('  resolve: %s' % reply)
                return

        # issue not found - create
        body  = 'problem: %s' % excuse()
        issue = repo.create_issue(title=title, body=body, labels=[labels],
                                  assignee=user)
        print('  issue  : %s' % title)
        print('  problem: %s' % body)
        print('----------------------------------------------------')


# ------------------------------------------------------------------------------

