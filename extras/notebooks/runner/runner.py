#!/usr/bin/python3
# -*- coding: utf-8 -*-
# pylint: disable=line-too-long

import argparse
from rich import print
import os

from snowflake.snowpark import Session
from snowflake.snowpark.functions import sproc
import snowpark_extensions

print("[cyan]Snowpark Extensions Extras")
print("[cyan]Notebook Runner")
print("[cyan]=============================")
arguments = argparse.ArgumentParser()
arguments.add_argument("--notebook",help="Jupyter Notebook to run")
arguments.add_argument("--registerproc",default="",help="Register an stored proc that can then be used to run notebooks")
arguments.add_argument("--stage",help="stage",default="NOTEBOOK_RUN")
arguments.add_argument("--packages",help="packages",default="")
arguments.add_argument("--imports" ,help="imports" ,default="")
arguments.add_argument("--connection",dest="connection_args",nargs="*",required=True,help="Connect options, for example snowsql, snowsql connection,env")


args = arguments.parse_args()
print(args)
session = None
try:
    if len(args.connection_args) >= 1:
        first_arg = args.connection_args[0]
        rest_args = args.connection_args[1:]
        if first_arg == "snowsql":
            session = Session.builder.from_snowsql(*rest_args).create()
        elif first_arg == "env":
            session = Session.builder.from_env().create()
        else:
            connection_args={}
            for arg in args.connection_args:
                key, value = arg.split("=")
                connection_args[key] = value
            session = Session.builder.configs(connection_args).create()
except Exception as e:
    print(e)
    print("[red] An error happened while trying to connect")
    exit(1)
if not session:
    print("[red] Not connected. Aborting")
    exit(2)
session.sql(f"CREATE STAGE IF NOT EXISTS {args.stage}").show()
print(f"Uploading notebook to stage {args.stage}")
session.file.put(f"file://{args.notebook}",f'@{args.stage}',auto_compress=False,overwrite=True)
print(f"Notebook uploaded")

packages=["snowflake-snowpark-python","nbconvert","nbformat","ipython","jinja2==3.0.3","plotly"]
packages.extend(set(filter(None, args.packages.split(','))))
print(f"Using packages [magenta]{packages}")
imports=[]
if args.imports:
    imports.extend(args.imports.split(','))
is_permanent=False
@sproc(name=args.registerproc,replace=True,is_permanent=is_permanent,packages=packages,imports=[])
def run_notebook(session:Session,stage:str,notebook_filename:str) -> str:
        # (c) Matthew Wardrop 2019; Licensed under the MIT license
        #
        # This script provides the ability to run a notebook in the same Python
        # process as this script, allowing it to access to variables created
        # by the notebook for other purposes. In most cases, this is of limited
        # utility and not a best-practice, but there are some limited cases in
        # which this capability is valuable, and this script was created for
        # such cases. For all other cases, you are better off using the
        # `nbconvert` execution API found @:
        # https://nbconvert.readthedocs.io/en/latest/execute_api.html
        import contextlib
        import io
        import logging
        import sys
        import traceback
        from base64 import b64encode
        import plotly
        import nbformat
        from IPython.core.formatters import format_display_data
        from IPython.terminal.interactiveshell import InteractiveShell
        from IPython.core.profiledir import ProfileDir

        class TeeOutput:
            def __init__(self, *orig_files):
                self.captured = io.StringIO()
                self.orig_files = orig_files
            def __getattr__(self, attr):
                return getattr(self.captured, attr)
            def write(self, data):
                self.captured.write(data)
                for f in self.orig_files:
                    f.write(data)
            def get_output(self):
                self.captured.seek(0)
                return self.captured.read()

        @contextlib.contextmanager
        def redirect_logging(fh):
            old_fh = {}
            for handler in logging.getLogger().handlers:
                if isinstance(handler, logging.StreamHandler):
                    old_fh[id(handler)] = handler.stream
                    handler.stream = fh
            yield
            for handler in logging.getLogger().handlers:
                if id(handler) in old_fh:
                    handler.stream = old_fh[id(handler)]
        class NotebookRunner:
            def __init__(self, namespace=None):
                pd = ProfileDir.create_profile_dir("/tmp/profile")
                self.shell = InteractiveShell(ipython_dir="/tmp/IPython",profile_dir=pd,user_ns=namespace)
            @property
            def user_ns(self):
                return self.shell.user_ns
            def run(self, nb, as_version=None, output=None, stop_on_error=True):
                if isinstance(nb, nbformat.NotebookNode):
                    nb = nb.copy()
                elif isinstance(nb, str):
                    nb = nbformat.read(nb, as_version=as_version)
                else:
                    raise ValueError(f"Unknown notebook reference: `{nb}`")
                # Clean notebook
                for cell in nb.cells:
                    cell.execution_count = None
                    cell.outputs = []
                # Run all notebook cells
                for cell in nb.cells:
                    if not self._run_cell(cell) and stop_on_error:
                        break
                # Output the notebook if request
                if output is not None:
                    nbformat.write(nb, output)
                return nb
            def _run_cell(self, cell):
                if cell.cell_type != 'code':
                    return cell
                cell.outputs = []
                # Actually run the cell code
                stdout = TeeOutput(sys.stdout)
                stderr = TeeOutput(sys.stderr)
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), redirect_logging(stderr):
                    result = self.shell.run_cell(cell.source, store_history=True)
                # Record the execution count on the cell
                cell.execution_count = result.execution_count
                # Include stdout and stderr streams
                for stream, captured in {
                    'stdout': self._strip_stdout(cell, stdout.get_output()),
                    'stderr': stderr.get_output()
                }.items():
                    if stream == 'stdout':
                        captured = self._strip_stdout(cell, captured)
                    if captured:
                        cell.outputs.append(nbformat.v4.new_output('stream', name=stream, text=captured))
                # Include execution results
                if result.result is not None:
                    if isinstance(result.result, plotly.graph_objects.Figure):
                        fig = result.result
                        # we render as html because kaleido and orca do not work
                        # inside of snowflake
                        html = fig.to_html(full_html=False, include_plotlyjs=False)
                        # convert graph to JSON
                        #fig_json = fig.to_json()
                        # convert graph to PNG and encode it
                        #png = plotly.io.to_image(fig)
                        #png_base64 = b64encode(png).decode('ascii')
                        #cell.outputs.append(nbformat.v4.new_output('display_data',{'image/png': png_base64}))
                        cell.outputs.append(nbformat.v4.new_output('display_data',{'text/html': html}))
                    else:
                        cell.outputs.append(nbformat.v4.new_output(
                            'execute_result', execution_count=result.execution_count, data=format_display_data(result.result)[0]
                        ))
                elif result.error_in_exec:
                    cell.outputs.append(nbformat.v4.new_output(
                        'error',
                        ename=result.error_in_exec.__class__.__name__,
                        evalue=result.error_in_exec.args[0],
                        traceback=self._render_traceback(
                            result.error_in_exec.__class__.__name__,
                            result.error_in_exec.args[0],
                            sys.last_traceback
                        )
                    ))
                return result.error_in_exec is None
            def _strip_stdout(self, cell, stdout):
                if stdout is None:
                    return
                idx = max(
                    stdout.find(f'Out[{cell.execution_count}]: '),
                    stdout.find("---------------------------------------------------------------------------")
                )
                if idx > 0:
                    stdout = stdout[:idx]
                return stdout
            def _render_traceback(self, etype, value, tb):
                """
                This method is lifted from `InteractiveShell.showtraceback`, extracting only
                the functionality needed by this runner.
                """
                try:
                    stb = value._render_traceback_()
                except Exception:
                    stb = self.shell.InteractiveTB.structured_traceback(etype, value, tb, tb_offset=None)
                return stb    
        import logging
        import zipfile
        import sys
        import os
        import shutil
        import nbformat
        import datetime
        from nbconvert.exporters import HTMLExporter
        def copy2(src, dst, *, follow_symlinks=True):
            if os.path.isdir(dst):
                dst = os.path.join(dst, os.path.basename(src))
            shutil.copyfile(src, dst, follow_symlinks=False)
            #copymode(src, dst, follow_symlinks=follow_symlinks)
            return dst
        shutil.copy = copy2
        IMPORT_DIRECTORY_NAME = "snowflake_import_directory"
        import_dir = sys._xoptions[IMPORT_DIRECTORY_NAME]
        os.makedirs("/tmp",exist_ok=True)
        notebook_name = os.path.basename(notebook_filename)
        target_name = ""
        try:
            exporter = HTMLExporter()
            notebook_filename=notebook_name
            session.file.get(f'@{stage}/{notebook_filename}','/tmp/')
            with open(f'/tmp/{notebook_filename}') as f:
                nb = nbformat.read(f, as_version=4)
            nr = NotebookRunner()
            output_nb = nr.run(nb, as_version=4)
            html_data, resources = exporter.from_notebook_node(output_nb)
            target_name = f"{notebook_name}_{datetime.datetime.now().strftime('%m_%d_%Y_%H_%M_%S')}.html"
            with open(f"/tmp/{target_name}", "wb") as f:
                # include plotly lib
                html_data= html_data.replace("<head>",'<head><script src="https://cdn.plot.ly/plotly-latest.min.js"></script>')
                f.write(html_data.encode('utf-8'))
                f.close()
            session.file.put(f"/tmp/{target_name}",stage_location=stage, auto_compress=False, overwrite=True)
            return f"@{stage}/{target_name}"
        except Exception as e:
            out_nb = None
            logging.error(e)
            raise e
        finally:
            logging.info(f"Execution of notebook  {notebook_filename} finished")
        return "Done! check " + target_name
print(f"STAGE: [cyan]{args.stage}")
result = run_notebook(args.stage,args.notebook)
print(f"Results have been written to {result}")
downloaded_results = session.file.get(result,"file://.")
print("Downloading results to local folder")
for downloaded_result in downloaded_results:
    print(f"Downloaded {downloaded_result.file}")
    target_file = downloaded_result.file.replace(".gz","")
print("Done!")