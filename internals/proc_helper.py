# -~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-
# ProcHelper: Process execution helper class.
# -~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

import errno
import io
import json
import subprocess
import os
import os.path

import sublime

import SublimeHaskell.sublime_haskell_common as Common
import SublimeHaskell.internals.logging as Logging
import SublimeHaskell.internals.settings as Settings
import SublimeHaskell.internals.utils as Utils
import SublimeHaskell.internals.which as Which
import SublimeHaskell.internals.cabal_cfgrdr as CabalConfigRdr

class ProcHelper(object):
    """Command and tool process execution helper."""

    # Augmented environment for the subprocesses. Specifically, we really want
    # to augment the user's PATH used to search for executables and tools:
    augmented_env = None

    def __init__(self, command, **popen_kwargs):
        """Open a pipe to a command or tool."""

        if ProcHelper.augmented_env is None:
            ProcHelper.augmented_env = ProcHelper.make_extended_env()

        self.process = None
        self.process_err = None

        if Utils.is_windows():
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            popen_kwargs['startupinfo'] = startupinfo

        # Allow caller to specify something different for stdout or stderr -- provide
        # the default here if unspecified.
        if popen_kwargs.get('stdout') is None:
            popen_kwargs['stdout'] = subprocess.PIPE
        if popen_kwargs.get('stderr') is None:
            popen_kwargs['stderr'] = subprocess.PIPE

        try:
            normcmd = Which.which(command, ProcHelper.augmented_env['PATH'])
            if normcmd is not None:
                self.process = subprocess.Popen(normcmd
                                                , stdin=subprocess.PIPE
                                                , env=ProcHelper.augmented_env
                                                , **popen_kwargs)
            else:
                self.process = None
                self.process_err = "SublimeHaskell.ProcHelper: {0} was not found on PATH!".format(command[0])

        except OSError as os_exc:
            self.process_err = \
                '\n'.join(["SublimeHaskell: Problem executing '{0}'".format(' '.join(command))
                           , 'Operating system error: {0}'.format(os_exc)
                          ])

            if os_exc.errno == errno.EPIPE:
                # Most likely reason: subprocess output a usage message
                stdout, stderr = self.process.communicate()
                exit_code = self.process.wait()
                self.process_err = self.process_err + \
                    '\n'.join([''
                               , 'Process exit code: {0}'.format(exit_code)
                               , ''
                               , "output:"
                               , stdout if stdout and len(stdout) > 0 else "--no output--"
                               , ''
                               , 'error:'
                               , stderr if stderr and len(stderr) > 0 else "--no error output--"])
                self.process = None
            else:
                self.process = None
                raise os_exc

    # 'with' statement support:
    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.cleanup()
        return False

    def cleanup(self):
        if self.process is not None:
            self.process.stdin.close()
            self.process.stdout.close()
            if self.process.stderr is not None:
                # stderr can be None if it is tied to stdout (i.e., 'stderr=subprocess.STDOUT')
                self.process.stderr.close()

    def wait(self, input_str=None):
        """Wait for subprocess to complete and exit, collect and decode ``stdout`` and ``stderr``,
        returning the tuple ``(exit_code, stdout, stderr)```"""
        if self.process is not None:
            stdout, stderr = self.process.communicate(Utils.encode_bytes(input_str) if input_str is not None else '')
            exit_code = self.process.wait()
            # Ensure that we reap the file descriptors.
            self.cleanup()
            return (exit_code, Utils.decode_bytes(stdout), Utils.decode_bytes(stderr))
        else:
            return (-1, '', self.process_err or "?? unknown error -- no process.")

    # Update the augmented environment when `add_to_PATH` or `add_standard_dirs` change.
    @staticmethod
    def update_environment(_key, _val):
        # Reinitialize the tool -> path cache:
        Which.reset_cache()
        ProcHelper.augmented_env = ProcHelper.make_extended_env()

    # Generate the augmented environment for subprocesses. This copies the
    # current process environment and updates PATH with `add_to_PATH` extras.
    @staticmethod
    def make_extended_env():

        ext_env = dict(os.environ)
        env_path = os.getenv('PATH') or ""
        std_places = []
        if Settings.PLUGIN.add_standard_dirs:
            std_places = ["$HOME/.local/bin" if not Utils.is_windows() else "%APPDATA%/local/bin"] + \
                         CabalConfigRdr.cabal_config()
            std_places = list(filter(os.path.isdir, map(Utils.normalize_path, std_places)))

        add_to_path = list(filter(os.path.isdir, map(Utils.normalize_path, Settings.PLUGIN.add_to_path, [])))

        Logging.log("std_places = {0}".format(std_places), Logging.LOG_INFO)
        Logging.log("add_to_PATH = {0}".format(add_to_path), Logging.LOG_INFO)

        ext_env['PATH'] = os.pathsep.join(add_to_path + std_places + [env_path])
        return ext_env

    @staticmethod
    def get_extended_env():
        if ProcHelper.augmented_env is None:
            ProcHelper.augmented_env = ProcHelper.make_extended_env()
        return ProcHelper.augmented_env

    @staticmethod
    def run_process(command, input_string='', **popen_kwargs):
        """Execute a subprocess, wait for it to complete, returning a ``(exit_code, stdout, stderr)``` tuple."""
        with ProcHelper(command, **popen_kwargs) as proc:
            return proc.wait(input_string)

    @staticmethod
    def invoke_tool(command, tool_name, inp='', on_result=None, filename=None, on_line=None, check_enabled=True,
                    **popen_kwargs):
        if check_enabled and not Settings.PLUGIN.__getattribute__(Utils.tool_enabled(tool_name)):
            return None

        source_dir = get_source_dir(filename)

        def mk_result(result):
            return on_result(result) if on_result else result

        try:
            with ProcHelper(command, cwd=source_dir, **popen_kwargs) as proc:
                exit_code, stdout, stderr = proc.wait(inp)
                if exit_code != 0:
                    raise Exception('{0} exited with exit code {1} and stderr: {2}'.format(tool_name, exit_code, stderr))

                if on_line:
                    for line in io.StringIO(stdout):
                        on_line(mk_result(line))
                else:
                    return mk_result(stdout)

        except OSError as os_exc:
            if os_exc.errno == errno.ENOENT:
                errmsg = "SublimeHaskell: {0} was not found!\n'{1}' is set to False".format(tool_name,
                                                                                            Utils.tool_enabled(tool_name))
                Common.output_error_async(sublime.active_window(), errmsg)
                Settings.PLUGIN.__setattr__(Utils.tool_enabled(tool_name), False)
            else:
                Logging.log('{0} fails with {1}, command: {2}'.format(tool_name, os_exc, command), Logging.LOG_ERROR)

            return None

        return None


def get_source_dir(filename):
    """
    Get root of hs-source-dirs for filename in project
    """
    if not filename:
        return os.path.expanduser('~')
        # return os.getcwd()

    cabal_dir, _ = Common.get_cabal_project_dir_and_name_of_file(filename)
    if not cabal_dir:
        return os.path.dirname(filename)

    _, cabal_file = Common.get_cabal_in_dir(cabal_dir)
    exit_code, out, _ = ProcHelper.run_process(['hsinspect', cabal_file])

    if exit_code == 0:
        info = json.loads(out)

        dirs = ["."]

        if 'error' not in info and 'description' in info:
            # collect all hs-source-dirs
            descr = info['description']
            if descr['library']:
                dirs.extend(descr['library']['info']['source-dirs'])
            for i in descr['executables']:
                dirs.extend(i['info']['source-dirs'])
            for test in descr['tests']:
                dirs.extend(test['info']['source-dirs'])

        paths = [os.path.abspath(os.path.join(cabal_dir, d)) for d in dirs]
        paths.sort(key=lambda p: -len(p))

        for path in paths:
            if filename.startswith(path):
                return path

    return os.path.dirname(filename)
