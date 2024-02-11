#!/usr/bin/env python3
#dragon

USE_DRAGON = True

import os
import sys
import enum
import queue
import signal

if USE_DRAGON:
    import dragon                  # pylint: disable=unused-import, import-error

import multiprocessing as mp
import threading       as mt

import radical.utils   as ru
import radical.pilot   as rp

# ------------------------------------------------------------------------------
#
class Server(object):

    class Command(enum.Enum):
        TO_WATCH  = 1
        TO_CANCEL = 2


    # --------------------------------------------------------------------------
    #
    def __init__(self):

        self._log = ru.Logger('radical.pilot.dragon')

        self._pin  = ru.zmq.Pipe(ru.zmq.MODE_PULL)
        self._pout = ru.zmq.Pipe(ru.zmq.MODE_PUSH)

        sys.stdout.write('ZMQ_ENDPOINTS %s %s\n'
                         % (ru.as_string(self._pin.url),
                            ru.as_string(self._pout.url)))
        sys.stdout.flush()

        with ru.ru_open('/tmp/dragon.endpoints', 'w') as fout:
            fout.write('%s\n' % ru.as_string(self._pin.url))
            fout.write('%s\n' % ru.as_string(self._pout.url))

        self._log.debug('ZMQ_ENDPOINTS %s %s', self._pin.url, self._pout.url)

        self._watch_queue = queue.Queue()
        self._done_queue  = queue.Queue()

        self._watcher = mt.Thread(target=self._watch)
        self._watcher.daemon = True
        self._watcher.start()


    # --------------------------------------------------------------------------
    #
    def serve(self):

        # FIXME: profile events
        self._log.info('serving')

        if USE_DRAGON:
            mp.set_start_method("dragon")

        self._log.info('serving dragon')

        try:
            while True:

                self._log.debug('main loop')

                self._handle_requests()
                self._handle_results()

        except Exception as e:
            self._log.exception('error in main loop: %s', e)
            raise


    # --------------------------------------------------------------------------
    #
    def _handle_requests(self):
        '''
        handle incoming requests from the parent process

        The requests are expected to be dictionaries with the following keys:
            - cmd:  the command to execute (run, cancel, stop)
            - task: the task to execute the command on
        '''

        self._log.debug('waiting for request')

        msg = self._pin.get_nowait(1.0)

        import pprint
        self._log.debug('got request %s', pprint.pformat(msg))

        if not msg:
            return

        if not isinstance(msg, dict):
            self._log.error('invalid message type %s', type(msg))

        cmd  = msg.get('cmd')
        task = msg.get('task')

        if cmd not in ['run', 'cancel', 'stop']:
            self._log.error('unsupported command %s', cmd)
            return

        if cmd == 'stop':
            self._log.info('stopping')
            sys.exit(0)

        if not task:
            self._log.error('no task in message')
            return

        if cmd == 'cancel':
            self._log.debug('cancel task %s', task['uid'])
            self._watch_queue.put((self.Command.TO_CANCEL, task))
            return

        self._log.debug('launch task %s', task['uid'])
        task['proc'] = mp.Process(target=self._fork_task, args=[task])
        task['proc'].start()
        self._log.debug('task %s launched', task['uid'])

        # FIXME: this should be done in the watcher
        # self.handle_timeout(task)

        # watch task for completion
        self._watch_queue.put((self.Command.TO_WATCH, task))

        self._log.debug('task %s watched', task['uid'])


    # --------------------------------------------------------------------------
    #
    def _handle_results(self):

        if self._done_queue.empty():
            return

        task = self._done_queue.get()
        self._pout.put(task)


    # --------------------------------------------------------------------------
    #
    def _fork_task(self, task):

        tid  = task['uid']
        sbox = task['task_sandbox_path']

        launch_script = '%s.launch.sh' % tid
        launch_out    = '%s/%s.launch.out' % (sbox, tid)
        launch_err    = '%s/%s.launch.err' % (sbox, tid)
        launch_cmd    = '%s/%s > %s 2> %s' % (sbox, launch_script,
                                                    launch_out, launch_err)
        out, err, ret = ru.sh_callout(launch_cmd, shell=True)

        # # redirect stdio of the current process to files
        # os.dup2(_launch_out_h.fileno(), sys.stdout.fileno())
        # os.dup2(_launch_err_h.fileno(), sys.stderr.fileno())

        # os.execvp(cmdline, [cmdline])


    # --------------------------------------------------------------------------
    #
    def _watch(self):

        to_watch  = list()
        to_cancel = list()

        # get completed tasks
        while True:

            if not self._watch_queue.empty():
                cmd, task = self._watch_queue.get()

                if cmd == self.Command.TO_WATCH:
                    to_watch.append(task)
                elif cmd == self.Command.TO_CANCEL:
                    to_cancel.append(task)
                else:
                    raise ValueError('unsupported cmd %s' % cmd)

            self._check_running(to_watch, to_cancel)


    # --------------------------------------------------------------------------
    # Iterate over all running tasks, check their status, and decide on the
    # next step.  Also check for a requested cancellation for the tasks.
    def _check_running(self, to_watch, to_cancel):

        # `to_watch.remove()` in the loop requires copy to iterate over the list
        for task in list(to_watch):

            tid  = task['uid']
            proc = task['proc']

            tasks_to_advance = list()
            tasks_to_cancel  = list()

            if proc.is_alive():

                # process is still running - cancel if needed
                if tid in to_cancel:

                    # got a request to cancel this task - send SIGTERM to the
                    # process group (which should include the actual launch
                    # method)
                    try:
                        # kill the whole process group
                        proc.kill()

                    except OSError:
                        # lost race: task is already gone, we ignore this
                        # FIXME: collect and move to DONE/FAILED
                        pass

                    proc.join()

                    to_cancel.remove(tid)
                    to_watch.remove(task)
                    del task['proc']  # proc is not json serializable

                    tasks_to_cancel.append(task)

            else:

                # make sure proc is collected
                proc.join()
                task['exit_code'] = proc.exitcode

                # Free the Slots, Flee the Flots, Ree the Frots!
                to_watch.remove(task)
                if tid in to_cancel:
                    to_cancel.remove(tid)

                del task['proc']  # proc is not json serializable
                tasks_to_advance.append(task)

                if proc.exitcode != 0:
                    # task failed - fail after staging output
                    task['exception']        = 'RuntimeError("task failed")'
                    task['exception_detail'] = 'exit code: %s' % proc.exitcode
                    task['target_state'    ] = rp.FAILED

                else:
                    # The task finished cleanly, see if we need to deal with
                    # output data.  We always move to stageout, even if there
                    # are no directives -- at the very least, we'll upload
                    # stdout/stderr
                    task['target_state'] = rp.DONE

                self._done_queue.put({'cmd' : 'done',
                                      'task': task})


# ------------------------------------------------------------------------------
#
if __name__ == '__main__':

    sys.stdout.write('started\n')
    sys.stdout.flush()

    s = Server()
    s.serve()


# ------------------------------------------------------------------------------

