"""Integration tests for phonon flows."""
from __future__ import print_function, division, unicode_literals, absolute_import

import pytest
import numpy as np
import numpy.testing.utils as nptu
import abipy.data as abidata
import abipy.abilab as abilab
import abipy.flowtk as flowtk

from abipy.core.testing import has_abinit, has_matplotlib


# Tests in this module require abinit >= 7.9.0 and pseudodojo.
#pytestmark = pytest.mark.skipif(not has_abinit("7.9.0"), reason="Requires abinit >= 7.9.0")


def scf_ph_inputs(tvars=None):
    """
    This function constructs the input files for the phonon calculation:
    GS input + the input files for the phonon calculation.
    """
    # Crystalline AlAs: computation of the second derivative of the total energy
    structure = abidata.structure_from_ucell("AlAs")

    # List of q-points for the phonon calculation (4,4,4) mesh.
    qpoints = [
             0.00000000E+00,  0.00000000E+00,  0.00000000E+00,
             2.50000000E-01,  0.00000000E+00,  0.00000000E+00,
             5.00000000E-01,  0.00000000E+00,  0.00000000E+00,
             2.50000000E-01,  2.50000000E-01,  0.00000000E+00,
             5.00000000E-01,  2.50000000E-01,  0.00000000E+00,
            -2.50000000E-01,  2.50000000E-01,  0.00000000E+00,
             5.00000000E-01,  5.00000000E-01,  0.00000000E+00,
            -2.50000000E-01,  5.00000000E-01,  2.50000000E-01,
            ]

    qpoints = np.reshape(qpoints, (-1,3))

    # Global variables used both for the GS and the DFPT run.
    global_vars = dict(nband=4,
                       ecut=3.0,
                       ngkpt=[4, 4, 4],
                       shiftk=[0, 0, 0],
                       tolvrs=1.0e-6,
                       paral_kgb=0 if tvars is None else tvars.paral_kgb,
                    )

    multi = abilab.MultiDataset(structure=structure, pseudos=abidata.pseudos("13al.981214.fhi", "33as.pspnc"),
                              ndtset=1+len(qpoints))

    multi.set_vars(global_vars)

    for i, qpt in enumerate(qpoints):
        # Response-function calculation for phonons.
        multi[i+1].set_vars(
            nstep=20,
            rfphon=1,        # Will consider phonon-type perturbation
            nqpt=1,          # One wavevector is to be considered
            qpt=qpt,         # This wavevector is q=0 (Gamma)
            kptopt=3,
            )

            #rfatpol   1 1   # Only the first atom is displaced
            #rfdir   1 0 0   # Along the first reduced coordinate axis
            #kptopt   2      # Automatic generation of k points, taking

    # Split input into gs_inp and ph_inputs
    return multi.split_datasets()


def itest_phonon_flow(fwp, tvars):
    """
    Create an `Abinit` for phonon calculations:

        1) One work for the GS run.

        2) nqpt works for phonon calculations. Each work contains
           nirred tasks where nirred is the number of irreducible phonon perturbations
           for that particular q-point.
    """
    if tvars.paral_kgb == 1:
        pytest.xfail("Phonon flow with paral_kgb==1 is expected to fail (implementation problem)")

    all_inps = scf_ph_inputs(tvars)
    scf_input, ph_inputs = all_inps[0], all_inps[1:]

    flow = flowtk.phonon_flow(fwp.workdir, scf_input, ph_inputs, manager=fwp.manager)
    flow.build_and_pickle_dump(abivalidate=True)

    t0 = flow[0][0]
    t0.start_and_wait()

    assert t0.uses_paral_kgb(tvars.paral_kgb)

    flow.check_status()
    assert t0.status == t0.S_OK
    flow.show_status()

    for work in flow[1:]:
        for task in work:
            task.start_and_wait()
            assert task.status == t0.S_DONE

    flow.check_status(show=True)

    # We should have a DDB files with IFC(q) in work.outdir
    ddb_files = []
    for work in flow[1:]:
        ddbs = work.outdir.list_filepaths(wildcard="*DDB")
        assert len(ddbs) == 1
        ddb_files.append(ddbs[0])

    assert all(work.finalized for work in flow)
    assert flow.all_ok

    # Merge the DDB files
    out_ddb = flow.outdir.path_in("flow_DDB")
    ddb_path = flowtk.Mrgddb().merge(flow.outdir.path, ddb_files, out_ddb=out_ddb,
                                     description="DDB generated by %s" % __file__)
    assert ddb_path == out_ddb

    # Test PhononTask inspect method
    ph_task = flow[1][0]

    # paral_kgb does not make sense for DFPT!
    assert not ph_task.uses_paral_kgb(tvars.paral_kgb)

    if has_matplotlib():
        ph_task.inspect(show=False)

    # Test get_results
    ph_task.get_results()

    # Build new work with Anaddb tasks.
    # Construct a manager with mpi_procs==1 since anaddb do not support mpi_procs > 1 (except in elphon)
    shell_manager = fwp.manager.to_shell_manager(mpi_procs=1)
    awork = flowtk.Work(manager=shell_manager)

    # Phonons bands and DOS with gaussian method
    anaddb_input = abilab.AnaddbInput.phbands_and_dos(
        scf_input.structure, ngqpt=(4, 4, 4), ndivsm=5, nqsmall=10, dos_method="gaussian: 0.001 eV")

    atask = flowtk.AnaddbTask(anaddb_input, ddb_node=ddb_path, manager=shell_manager)
    awork.register(atask)

    # Phonons bands and DOS with tetrahedron method
    anaddb_input = abilab.AnaddbInput.phbands_and_dos(
        scf_input.structure, ngqpt=(4, 4, 4), ndivsm=5, nqsmall=10, dos_method="tetra")

    atask = flowtk.AnaddbTask(anaddb_input, ddb_node=ddb_path, manager=shell_manager)
    awork.register(atask)

    flow.register_work(awork)
    flow.allocate()
    flow.build()

    for i, atask in enumerate(awork):
        atask.history.info("about to run anaddb task: %d", i)
        atask.start_and_wait()
        assert atask.status == atask.S_DONE
        atask.check_status()
        assert atask.status == atask.S_OK

        # TODO: output files are not produced in outdir
        #assert len(atask.outdir.list_filepaths(wildcard="*PHBST.nc")) == 1
        #assert len(atask.outdir.list_filepaths(wildcard="*PHDOS.nc")) == 1

    #assert flow.validate_json_schema()


def itest_phonon_restart(fwp):
    """Test the restart of phonon calculations with the scheduler."""
    # Crystalline AlAs: computation of the second derivative of the total energy
    structure = abidata.structure_from_ucell("AlAs")

    # List of q-points for the phonon calculation (4,4,4) mesh.
    qpoints = np.reshape([
             0.00000000E+00,  0.00000000E+00,  0.00000000E+00,
             2.50000000E-01,  0.00000000E+00,  0.00000000E+00,
             #5.00000000E-01,  0.00000000E+00,  0.00000000E+00,  # XXX Uncomment this line to test restart from 1DEN
                                                                 # Too long --> disabled
            ], (-1, 3))

    # Global variables used both for the GS and the DFPT run.
    global_vars = dict(nband=4,
                       ecut=3.0,
                       ngkpt=[4, 4, 4],
                       shiftk=[0, 0, 0],
                       tolvrs=1.0e-5,
                    )

    multi = abilab.MultiDataset(structure=structure, pseudos=abidata.pseudos("13al.981214.fhi", "33as.pspnc"),
                                ndtset=1+len(qpoints))

    multi.set_vars(global_vars)

    for i, qpt in enumerate(qpoints):
        # Response-function calculation for phonons.
        multi[i+1].set_vars(
            rfphon=1,        # Will consider phonon-type perturbation.
            nqpt=1,          # One wavevector is to be considered.
            qpt=qpt,         # q-wavevector.
            kptopt=3,
            nstep=5,         # This is to trigger the phonon restart.
        )
        #rfatpol   1 1   # Only the first atom is displaced
        #rfdir   1 0 0   # Along the first reduced coordinate axis
        #kptopt   2      # Automatic generation of k points, taking

                                                           # i == 0 --> restart from WFK
        if i == 1: multi[i+1].set_vars(prtwf=-1, nstep=5)  # Restart with WFK and smart- io.
        if i == 2: multi[i+1].set_vars(prtwf=0, nstep=8)   # Restart from 1DEN. Too long --> disabled.

    all_inps = multi.split_datasets()
    scf_input, ph_inputs = all_inps[0], all_inps[1:]

    flow = flowtk.phonon_flow(fwp.workdir, scf_input, ph_inputs, manager=fwp.manager)
    flow.set_garbage_collector()

    for task in flow.iflat_tasks():
        task.manager.qadapter.max_num_launches = 20

    assert flow.make_scheduler().start() == 0

    flow.check_status(show=True, verbose=1)
    assert all(work.finalized for work in flow)
    assert flow.all_ok

    assert sum(task.num_restarts for task in flow.iflat_tasks()) > 0


def itest_oneshot_phonon_work(fwp):
    """
    Test build_oneshot_phononwork i.e. computation of the phonon frequencies
    for all modes in a single task.
    """
    all_inps = scf_ph_inputs()

    # SCF + one-shot for the first 2 qpoints.
    scf_input, ph_inputs = all_inps[0], all_inps[1:3]

    from pymatgen.io.abinit.works import build_oneshot_phononwork
    flow = flowtk.Flow(fwp.workdir)

    # rfdir and rfatpol are missing!
    with pytest.raises(ValueError):
        phon_work = build_oneshot_phononwork(scf_input, ph_inputs)
    for phinp in ph_inputs: phinp.set_vars(rfdir=[1, 1, 1])

    with pytest.raises(ValueError):
        phon_work = build_oneshot_phononwork(scf_input, ph_inputs)
    for phinp in ph_inputs: phinp.set_vars(rfatpol=[1, len(phinp.structure)])

    # Now ph_inputs is ok
    phon_work = build_oneshot_phononwork(scf_input, ph_inputs)
    flow.register_work(phon_work)

    assert flow.make_scheduler().start() == 0

    flow.check_status(show=True, verbose=1)
    assert all(work.finalized for work in flow)
    assert flow.all_ok

    # Read phonons from main output file.
    phonons_list = phon_work.read_phonons()
    assert len(phonons_list) == 2

    ph0, ph1 = phonons_list[0], phonons_list[1]
    assert ph0.qpt == [0, 0, 0]
    assert ph1.qpt == [2.50000000E-01,  0.00000000E+00,  0.00000000E+00]
    assert len(ph0.freqs) == 3 * len(scf_input.structure)
    assert len(ph1.freqs) == 3 * len(scf_input.structure)
    nptu.assert_almost_equal(ph0.freqs.to("Ha"),
        [-1.219120E-05, -1.201501E-05, -1.198453E-05,  1.577646E-03,  1.577647E-03, 1.577647E-03])
    nptu.assert_almost_equal(ph1.freqs.to("Ha"),
        [2.644207E-04, 2.644236E-04, 6.420904E-04, 1.532696E-03, 1.532697E-03, 1.707196E-03])

    #assert 0
