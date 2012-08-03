import hadoopy
import logging
import os
import multiprocessing
import sys
import Queue
import subprocess
import tempfile
import shutil


def _local_reader(worker_queue_maxsize, q_recv, q_send, in_r_fd, in_w_fd, out_r_fd, out_w_fd):
    os.close(in_r_fd)
    os.close(in_w_fd)
    os.close(out_w_fd)
    q = Queue.Queue()
    with hadoopy.TypedBytesFile(read_fd=out_r_fd) as tbfp_r:
        for num, kv in enumerate(tbfp_r):
            while True:
                while not q_recv.poll() and not q.empty():
                    q_send.send(q.get())
                try:
                    q.put_nowait(kv)
                except Queue.Full:
                    continue
                else:
                    break
    while not q.empty():
        q_send.send(q.get())
    q_send.close()


def launch_local(in_name, out_name, script_path, max_input=None,
                 files=(), cmdenvs=(), pipe=True, python_cmd='python', remove_tempdir=True,
                 worker_queue_maxsize=0, **kw):
    """A simple local emulation of hadoop

    This doesn't run hadoop and it doesn't support many advanced features, it
    is intended for simple debugging.  The input/output uses HDFS if an
    HDFS path is given. This allows for small tasks to be run locally
    (primarily while debugging). A temporary working directory is used and
    removed.

    Support

    * Environmental variables
    * Map-only tasks
    * Combiner
    * Files
    * Pipe (see below)
    * Display of stdout/stderr
    * Iterator of KV pairs as input or output (bypassing HDFS)

    :param in_name: Input path (string or list of strings) or Iterator of (key, value).  If it is an iterator then no input is taken from HDFS.
    :param out_name: Output path or None.  If None then output is not placed on HDFS, it is available through the 'output' key of the return value.
    :param script_path: Path to the script (e.g., script.py)
    :param max_input: Maximum number of Mapper inputs, None (default) then unlimited.
    :param files: Extra files (other than the script) (iterator).  NOTE: Hadoop copies the files into working directory
    :param cmdenvs: Extra cmdenv parameters (iterator)
    :param pipe: If true (default) then call user code through a pipe to isolate it and stop bugs when printing to stdout.  See project docs.
    :param python_cmd: The python command to use. The default is "python".  Can be used to override the system default python, e.g. python_cmd = "python2.6"
    :param remove_tempdir: If True (default), then rmtree the temporary dir, else print its location.  Useful if you need to see temporary files or how input files are copied.
    :param worker_queue_maxsize: The number of elements the queue holding results from the worker task will hold (default 0 which is unlimited).
    :rtype: Dictionary with some of the following entries (depending on options)
    :returns: freeze_cmds: Freeze command(s) ran
    :returns: frozen_tar_path: HDFS path to frozen file
    :returns: hadoop_cmds: Hadoopy command(s) ran
    :returns: process: subprocess.Popen object
    :returns: output: Iterator of (key, value) pairs
    :raises: subprocess.CalledProcessError: Hadoop error.
    :raises: OSError: Hadoop streaming not found.
    :raises: TypeError: Input types are not correct.
    :raises: ValueError: Script not found
    """
    if isinstance(files, (str, unicode)) or isinstance(cmdenvs, (str, unicode)) or ('cmdenvs' in kw and isinstance(kw['cmdenvs'], (str, unicode))):
        raise TypeError('files and cmdenvs must be iterators of strings and not strings!')
    cmdenvs = hadoopy._runner._listeq_to_dict(cmdenvs)
    logging.info('Local[%s]' % script_path)
    script_info = hadoopy._runner._parse_info(script_path, python_cmd)
    if not files:
        files = []
    else:
        files = list(files)
    files.append(script_path)
    files = [os.path.abspath(f) for f in files]
    # Setup env
    env = dict(os.environ)
    env['stream_map_input'] = 'typedbytes'
    env.update(cmdenvs)

    def _run_task(task, kvs, env):
        sys.stdout.flush()
        cur_max_input = max_input if task == 'map' else None
        if pipe:
            task = 'pipe %s' % task
        in_r_fd, in_w_fd = os.pipe()
        out_r_fd, out_w_fd = os.pipe()
        cmd = ('%s %s %s' % (python_cmd, script_path, task)).split()
        a = os.fdopen(in_r_fd, 'r')
        b = os.fdopen(out_w_fd, 'w')
        q_recv, q_send = multiprocessing.Pipe(True)
        reader_process = multiprocessing.Process(target=_local_reader, args=(worker_queue_maxsize, q_recv, q_send, in_r_fd, in_w_fd, out_r_fd, out_w_fd))
        reader_process.start()
        os.close(out_r_fd)
        q_send.close()
        try:
            p = subprocess.Popen(cmd,
                                 stdin=a,
                                 stdout=b,
                                 close_fds=True,
                                 env=env)
            a.close()
            b.close()
            # main =(in_fd)> p
            # main <(out_fd)= p
            with hadoopy.TypedBytesFile(write_fd=in_w_fd, flush_writes=True) as tbfp_w:
                for num, kv in enumerate(kvs):
                    if cur_max_input is not None and cur_max_input <= num:
                        break
                    if reader_process.exitcode:
                        raise EOFError('Reader process died[%s]' % reader_process.exitcode)
                    while q_recv.poll():
                        try:
                            yield q_recv.recv()
                        except EOFError:
                            break
                    tbfp_w.write(kv)
            # Get any remaining values
            while True:
                if reader_process.exitcode:
                    raise EOFError('Reader process died[%s]' % reader_process.exitcode)
                try:
                    yield q_recv.recv()
                except EOFError:
                    break
        finally:
            q_recv.close()
            reader_process.join()
            p.kill()
            p.wait()

    orig_pwd = os.path.abspath('.')
    new_pwd = tempfile.mkdtemp()
    out = {}
    try:
        logging.info('Hadoopy: Launch local changing current directory to [%s]' % new_pwd)
        os.chdir(new_pwd)
        if files:
            for f in files:
                shutil.copy(f, os.path.basename(f))
        script_path = os.path.basename(script_path)
        hadoopy._runner._make_script_executable(script_path)
        if isinstance(in_name, (str, unicode)) or (in_name and isinstance(in_name, (list, tuple)) and isinstance(in_name[0], (str, unicode))):
            in_kvs = hadoopy.readtb(in_name)
        else:
            in_kvs = in_name
        if 'reduce' in script_info['tasks']:
            kvs = list(_run_task('map', in_kvs, env))
            if 'combine' in script_info['tasks']:
                kvs = hadoopy.Test.sort_kv(kvs)
                kvs = list(_run_task('combine', kvs, env))
            kvs = hadoopy.Test.sort_kv(kvs)
            kvs = _run_task('reduce', kvs, env)
        else:
            kvs = _run_task('map', in_kvs, env)
    except OSError, e:
        logging.error('Ensure that [%s] starts with "#!/usr/bin/env python".' % script_path)
        raise e
    else:
        if out_name is not None:
            hadoopy.writetb(out_name, kvs)
            out['output'] = hadoopy.readtb(out_name)
        else:
            out['output'] = iter(list(kvs))  # TODO(brandyn): Potential problem if using large values, fixes changing dir early
        return out
    finally:
        os.chdir(orig_pwd)
        if remove_tempdir:
            shutil.rmtree(new_pwd)
        else:
            logging.info('Temporary directory not removed[%s]' % new_pwd)
