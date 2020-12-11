#!/usr/bin/env python

__copyright__ = 'Copyright 2013-2014, http://radical.rutgers.edu'
__license__ = 'MIT'

import os
import glob

import radical.pilot as rp
import radical.utils as ru

# ------------------------------------------------------------------------------
#
# READ the RADICAL-Pilot documentation: https://radicalpilot.readthedocs.io/
#
# ------------------------------------------------------------------------------


# ------------------------------------------------------------------------------
#
def test_bw_tagging():

    # we use a reporter class for nicer output
    report = ru.Reporter(name='radical.pilot')
    report.title('Getting Started (RP version %s)' % rp.version)

    # Create a new session. No need to try/except this: if session creation
    # fails, there is not much we can do anyways...
    session = rp.Session()

    # Add a Pilot Manager. Pilot managers manage one or more Pilots.
    pmgr = rp.PilotManager(session=session)

    # Define an [n]-core local pilot that runs for [x] minutes
    # Here we use a dict to initialize the description object
    pd_init = {'resource': 'ncsa.bw_aprun',
               'runtime': 10,  # pilot runtime (min)
               'exit_on_error': True,
               'project': 'gk4',
               'queue': 'high',
               'access_schema': 'gsissh',
               'cores': 128
              }
    pdesc = rp.PilotDescription(pd_init)

    # Launch the pilot.
    pilot = pmgr.submit_pilots(pdesc)

    report.header('submit tasks')

    # Register the Pilot in a TaskManager object.
    umgr = rp.TaskManager(session=session)
    umgr.add_pilots(pilot)

    # Create a workload of Tasks.
    # Each task runs '/bin/date'.

    n = 5  # number of tasks to run
    report.info('create %d task description(s)\n\t' % n)

    cuds = list()
    for i in range(0, n):

        # create a new Task description, and fill it.
        # Here we don't use dict initialization.
        cud                  = rp.TaskDescription()
        cud.executable       = '/bin/hostname'
        cud.arguments        = ['>', 's1_t%s_hostname.txt' % i]
        cud.cpu_processes    = 1
        cud.cpu_threads      = 16
      # cud.cpu_process_type = rp.MPI
      # cud.cpu_thread_type  = rp.OpenMP
        cud.output_staging   = {'source': 'task:///s1_t%s_hostname.txt' % i,
                                'target': 'client:///s1_t%s_hostname.txt' % i,
                                'action': rp.TRANSFER}
        cuds.append(cud)
        report.progress()
    report.ok('>>ok\n')

    # Submit the previously created Task descriptions to the
    # PilotManager. This will trigger the selected scheduler to start
    # assigning Tasks to the Pilots.
    cus = umgr.submit_tasks(cuds)

    # Wait for all tasks to reach a final state
    # (DONE, CANCELED or FAILED).
    report.header('gather results')
    umgr.wait_tasks()

    n = 5  # number of tasks to run
    report.info('create %d task description(s)\n\t' % n)

    cuds = list()
    for i in range(0, n):

        # create a new Task description, and fill it.
        # Here we don't use dict initialization.
        cud                  = rp.TaskDescription()
        cud.executable       = '/bin/hostname'
        cud.arguments        = ['>', 's2_t%s_hostname.txt' % i]
        cud.cpu_processes    = 1
        cud.cpu_threads      = 16
        cud.tag              = cus[i].uid
      # cud.cpu_process_type = rp.MPI
      # cud.cpu_thread_type  = rp.OpenMP
        cud.output_staging   = {'source': 'task:///s2_t%s_hostname.txt' % i,
                                'target': 'client:///s2_t%s_hostname.txt' % i,
                                'action': rp.TRANSFER}
        cuds.append(cud)
        report.progress()
    report.ok('>>ok\n')

    # Submit the previously created Task descriptions to the
    # PilotManager. This will trigger the selected scheduler to start
    # assigning Tasks to the Pilots.
    cus = umgr.submit_tasks(cuds)

    # Wait for all tasks to reach a final state (DONE, CANCELED or FAILED).
    report.header('gather results')
    umgr.wait_tasks()

    for i in range(0, n):
        assert open('s1_t%s_hostname.txt' % i,'r').readline().strip() == \
               open('s2_t%s_hostname.txt' % i,'r').readline().strip()

    report.header('finalize')
    session.close(download=True)

    report.header()

    for f in glob.glob('%s/*.txt' % os.getcwd()):
        os.remove(f)


# ------------------------------------------------------------------------------

