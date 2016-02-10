from __future__ import print_function, division, unicode_literals, absolute_import

import os
import ast
import types

from orderedset import OrderedSet
from monty.termcolor import cprint
from pymatgen.io.abinit.flows import Flow


def as_orderedset(token, options):
    """
    Helper function used to parse --mpi-list argument.
    Return OrderedSet
    """

    if token.startswith("range"):
        # start(1,4,2)
        token = token[5:]
        print("token", token)
        t = ast.literal_eval(token)
        assert len(t) == 3
        l = list(range(t[0], t[1], t[2]))

    elif token.endswith("x"):
        # 16x --> multiple of 16
        fact = int(token[:-1])
        l, i = [], 0
        while True:
            i += 1
            val = fact * i
            if val > options.max_ncpus: break
            l.append(val)

    elif token.startswith("pow"):
        # pow4 --> powers of 4
        base = int(token[3:])
        l, i = [], 0
        while True:
            i += 1
            val = base ** i
            if val > options.max_ncpus: break
            l.append(val)
    else:
        # lists
        l = ast.literal_eval(token)

    #print("l", l)
    return OrderedSet(l)


def bench_main(main):
    """
    This decorator is used to decorate main functions producing `AbinitFlows`.
    It adds the initialization of the logger and an argument parser that allows one to select 
    the loglevel, the workdir of the flow as well as the YAML file with the parameters of the `TaskManager`.
    The main function shall have the signature:

        main(options)

    where options in the container with the command line options generated by `ArgumentParser`.

    Args:
        main:
            main function.
    """
    from functools import wraps

    @wraps(main)
    def wrapper(*args, **kwargs):
        import argparse
        parser = argparse.ArgumentParser()

        parser.add_argument('--loglevel', default="ERROR", type=str,
                            help="set the loglevel. Possible values: CRITICAL, ERROR (default), WARNING, INFO, DEBUG")

        parser.add_argument('-v', '--verbose', default=0, action='count', # -vv --> verbose=2
                                  help='verbose, can be supplied multiple times to increase verbosity')

        parser.add_argument("-w", '--workdir', default="", type=str, help="Working directory of the flow.")

        parser.add_argument("-m", '--manager', default=None, 
                            help="YAML file with the parameters of the task manager. " 
                                 "Default None i.e. the manager is read from standard locations: "
                                 "working directory first then ~/.abinit/abipy/manager.yml.")

        parser.add_argument("--mpi-list", default=None, type=str, help="List of MPI processors to be tested. Syntax:\n"
                            "--mpi-list='[1,6,12]' to define a list, 'range(1,4,2)' for a python range.\n" 
                            "--mpi-list='16x' for multiple of 16 up to max--ncpus, --mpi-list='pow2' for powers of 2")
        parser.add_argument("--omp-list", default=None, type=str, help="List of OMP threads to be tested. Default is [1]. Same syntax as mpi-list.")

        parser.add_argument("--min-ncpus", default=-1, type=int, help="Minimum number of CPUs to be tested.")
        parser.add_argument("--max-ncpus", default=248, type=int, help="Maximum number of CPUs to be tested. Default: 248.")
        parser.add_argument("--min-eff", default=None, type=float, help="Minimum parallel efficiency accepted. Default None.")

        parser.add_argument('--paw', default=False, action="store_true", help="Run PAW calculation if available")
        parser.add_argument('--validate', default=False, action="store_true", help="Validate input files and return")

        parser.add_argument("-i", '--info', default=False, action="store_true", help="Show benchmark info and exit")
        parser.add_argument("-r", "--remove", default=False, action="store_true", help="Remove old flow workdir")

        parser.add_argument("--scheduler", "-s", default=False, action="store_true", help="Run with the scheduler")

        options = parser.parse_args()

        # loglevel is bound to the string value obtained from the command line argument. 
        # Convert to upper case to allow the user to specify --loglevel=DEBUG or --loglevel=debug
        import logging
        numeric_level = getattr(logging, options.loglevel.upper(), None)
        if not isinstance(numeric_level, int):
            raise ValueError('Invalid log level: %s' % options.loglevel)
        logging.basicConfig(level=numeric_level)

        # parse arguments
        if options.mpi_list is not None:
            options.mpi_list = as_orderedset(options.mpi_list, options)

        if options.omp_list is None:
            options.omp_list = [1]
        else:
            options.omp_list = as_orderedset(options.omp_list, options)

        # Monkey patch options to add useful method 
        def monkey_patch(opts):

            # options.accept_mpi_omp(mpi_proc, omp_threads)
            def accept_mpi_omp(opts, mpi_procs, omp_threads):
                """Return True if we can run a benchmark with mpi_procs and omp_threads"""
                tot_ncpus = mpi_procs * omp_threads

                if tot_ncpus < opts.min_ncpus:
                    if options.verbose:
                        cprint("Skipping %d because of min_ncpus" % tot_ncpus, color="magenta")
                    return False

                if opts.max_ncpus is not None and tot_ncpus > opts.max_ncpus:
                    if options.verbose:
                        cprint("Skipping %d because of max_ncpus" % tot_ncpus, color="magenta")
                    return False

                return True 

            opts.accept_mpi_omp = types.MethodType(accept_mpi_omp, opts)

            def accept_conf(opts, conf, omp_threads):
                """Return True if we can run a benchmark with mpi_procs and omp_threads"""
                tot_ncpus = conf.mpi_procs * omp_threads
                                                                                                      
                if tot_ncpus < opts.min_ncpus:
                    if options.verbose:
                        cprint("Skipping %d because of min_ncpus" % tot_ncpus, color="magenta")
                    return False
                                                                                                      
                if opts.max_ncpus is not None and tot_ncpus > opts.max_ncpus:
                    if options.verbose:
                        cprint("Skipping %d because of max_ncpus" % tot_ncpus, color="magenta")
                    return False

                if opts.min_eff is not None and conf.efficiency < opts.min_eff: 
                    if options.verbose:
                        cprint("Skipping %d because of parallel efficiency" % tot_ncpus, color="magenta")
                    return False

                if options.verbose:
                    cprint("Accepting omp_threads:%s with conf\n%s" % (omp_threads, conf), color="green")

                return True 

            opts.accept_conf = types.MethodType(accept_conf, opts)

            # options.get_workdir(__file__)
            def get_workdir(opts, _file_):
                """
                Return the workdir of the benchmark. 
                A default value if constructed from the name of the scrip if no cmd line arg.
                """
                if options.workdir: return options.workdir
                return "bench_" + os.path.basename(_file_).replace(".py", "")

            opts.get_workdir = types.MethodType(get_workdir, opts)
            
        monkey_patch(options)

        # Istantiate the manager.
        from abipy.abilab import TaskManager
        options.manager = TaskManager.as_manager(options.manager)

        flow = main(options)
        if flow is None: return 0

        if options.validate:
            # Validate inputs and return
            retcode = 0
            for task in flow.iflat_tasks():
                v = task.input.abivalidate()
                if v.retcode != 0: cprint(v, color="red")
                retcode += v.retcode 
            print("input validation retcode: %d" % retcode)
            return retcode

        if options.scheduler:
            return flow.make_scheduler().start()

        return 0

    return wrapper


class BenchmarkFlow(Flow):

    def exclude_from_benchmark(self, node):
        """Exclude a task or the tasks in a Work from the benchmark analysis."""
        if not hasattr(self, "_exclude_nodeids"): self._exclude_nodeids = set()

        if node.is_work:
            for task in node:
                self._exclude_nodeids.add(task.node_id)
        else:
            assert node.is_task
            self._exclude_nodeids.add(node.node_id)

    @property
    def exclude_nodeids(self):
        if not hasattr(self, "_exclude_nodeids"): self._exclude_nodeids = set()
        return self._exclude_nodeids 

    def get_parser(self):
        """
        Parse the timing sections in the output files.
        Return AbinitTimerParser parser object for further analysis.
        """
        nids = []
        for task in self.iflat_tasks():
            if task.node_id in self.exclude_nodeids: continue
            if task.status != task.S_OK: continue
            #print("analysing task:", task)
            nids.append(task.node_id)

        parser = self.parse_timing(nids=nids)

        if parser is None: 
            print("flow.parse_timing returned None!")
        else:
            if len(parser) != len(nids): 
                print("Not all timing sections have been parsed!")

        return parser

    def build_and_pickle_dump(self, **kwargs):
        cnt = 0
        for task in self.iflat_tasks():
            if task.node_id in self.exclude_nodeids: continue
            cnt += 1
            print("%s: mpi_procs %d, omp_threads %d" % 
              (task, task.manager.qadapter.mpi_procs, task.manager.qadapter.omp_threads))
        print("Total number of benchmarks: %d" % cnt)

        return super(BenchmarkFlow, self).build_and_pickle_dump(**kwargs)

    #def make_tarball(self):
    #    self.make_tarfile(self, name=None, max_filesize=None, exclude_exts=None, exclude_dirs=None, verbose=0, **kwargs):
