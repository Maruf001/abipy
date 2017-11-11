# coding: utf-8
"""Classes for the analysis of BSE calculations"""
from __future__ import print_function, division, unicode_literals, absolute_import

import sys
import os
import itertools
import numpy as np
import pandas as pd

from collections import OrderedDict
from monty.collections import AttrDict
from monty.functools import lazy_property
from monty.string import marquee, is_string
from abipy.tools.plotting import add_fig_kwargs, get_ax_fig_plt
from abipy.core.func1d import Function1D
from abipy.core.kpoints import Kpoint, KpointList
from abipy.core.mixins import AbinitNcFile, Has_Structure, NotebookWriter
from abipy.core.tensor import SymmetricTensor
from abipy.iotools import ETSF_Reader
from abipy.tools.plotting import set_axlims
from abipy.tools import duck
from abipy.abio.robots import Robot
from abipy.electrons.ebands import RobotWithEbands


__all__ = [
    "DielectricTensor",
    "DielectricFunction",
    "MdfFile",
    "MdfReader",
    "MdfPlotter",
    "MultipleMdfPlotter",
]


class DielectricTensor(object):
    """
    This object stores the frequency-dependent macroscopic dielectric tensor
    obtained from the dielectric functions for different q-directions.
    """
    def __init__(self, mdf, structure):
        nfreq = len(mdf.wmesh)

        self._wmesh = mdf.wmesh

        # Transform mdf emacros_q to numpy array
        all_emacros = []
        for emacro in mdf.emacros_q:
            all_emacros.append(emacro.values)

        all_emacros = np.array(all_emacros)

        # One tensor for each frequency
        all_tensors = []
        for ifrq, freq in enumerate(mdf.wmesh):
            tensor = SymmetricTensor.from_directions(mdf.qfrac_coords, all_emacros[:,ifrq],
                                                     structure.lattice.reciprocal_lattice, space="g")
            all_tensors.append(tensor)

        self._all_tensors = all_tensors

    def to_array(self, red_coords=True):
        """
        Return numpy array with a copy of the data.

        Args:
            red_coords: True for tensors in reduced coordinates else Cartesian.
        """
        table = []
        for tensor in self._all_tensors:
            if red_coords:
                table.append(tensor.reduced_tensor)
            else:
                table.append(tensor.cartesian_tensor)

        return np.array(table)

    def symmetrize(self, structure):
        """
        Symmetrize the tensor using the symmetry operations in structure.
        Change the object in place.
        """
        for tensor in self._all_tensors:
            tensor.symmetrize(structure)

    def to_func1d(self, red_coords=True):
        """Return list of Function."""
        table = self.to_array(red_coords)
        all_funcs = []

        for i in np.arange(3):
            for j in np.arange(3):
                all_funcs.append(Function1D(self._wmesh, table[:,i,j]))

        return all_funcs

    @add_fig_kwargs
    def plot(self, ax=None, *args, **kwargs):
        """
        Plot all the components of the tensor

        Args:
            ax: matplotlib `Axes` or None if a new figure should be created.

        ==============  ==============================================================
        kwargs          Meaning
        ==============  ==============================================================
        red_coords      True to plot the reduced coordinate tensor (Default: True)
        ==============  ==============================================================

        Returns:
            matplotlib figure
        """
        red_coords = kwargs.pop("red_coords", True)
        ax, fig, plt = get_ax_fig_plt(ax)

        ax.grid(True)
        ax.set_xlabel('Frequency [eV]')
        ax.set_ylabel('Dielectric tensor')

        #if not kwargs:
        #    kwargs = {"color": "black", "linewidth": 2.0}

        # Plot the 6 independent components
        for icomponent in [0, 4, 8, 1, 2, 5]:
            self.plot_ax(ax, icomponent, red_coords, *args, **kwargs)

        return fig

    def plot_ax(self, ax, what, red_coords, *args, **kwargs):
        """
        Helper function to plot data on the axis ax.

        Args:
            ax: plot axis
            what: Sequential index of the tensor matrix element.
            args: Positional arguments passed to ax.plot
            kwargs: Keyword arguments passed to matplotlib. Accepts also:

        ==============  ==============================================================
        kwargs          Meaning
        ==============  ==============================================================
        cplx_mode:      string defining the data to print (case-insensitive).
                        Possible choices are:

                            - "re"  for real part
                            - "im" for imaginary part only.
                            - "abs' for the absolute value

                        Options can be concated with "-".
        ==============  ==============================================================
        """
        # Extract the function to plot according to qpoint.
        if duck.is_intlike(what):
            f = self.to_func1d(red_coords)[int(what)]
        else:
            raise ValueError("Don't know how to handle %s" % str(what))

        return f.plot_ax(ax, *args, **kwargs)


class DielectricFunction(object):
    """
    This object stores the frequency-dependent macroscopic dielectric function
    computed for different q-directions in reciprocal space.

    .. note:

        Frequencies are in eV
    """

    def __init__(self, structure, qpoints, wmesh, emacros_q, info):
        """
        Args:
            structure: :class: Structure object.
            qpoints: :class:`KpointList` with the qpoints in reduced coordinates.
            wmesh: Array-like object with the frequency mesh (eV).
            emacros_q: Iterable with the macroscopic dielectric function for the different q-points.
            info: Dictionary containing info on the calculation that produced
                  the results (read from file). It must contain the following keywords:

                    - "lfe": True if local field effects are included.
                    - "calc_type": string defining the calculation type.

        """
        self.wmesh = np.array(wmesh)
        self.qpoints = qpoints
        assert len(self.qpoints) == len(emacros_q)
        self.info = info

        self.emacros_q, em_avg = [], np.zeros(len(wmesh), dtype=np.complex)
        for emq in emacros_q:
            em_avg += emq
            self.emacros_q.append(Function1D(wmesh, emq))
        self.emacros_q = tuple(self.emacros_q)

        # Compute the average value.
        # TODO: One should take into account the star of q, but I need the symops
        self.emacro_avg = Function1D(wmesh, em_avg / self.num_qpoints)

    def __str__(self):
        return self.to_string()

    def to_string(self, verbose=0, with_info=False):
        """String representation."""
        lines = []
        app = lines.append
        app(self.__class__.__name__)
        #app("calc_type: %s, has_lfe: %s, num_qpoints: %d" % (self.calc_type, self.has_lfe, self.num_qpoints))
        app("num_qpoints: %d" % (self.num_qpoints))
        if with_info or verbose:
            app(str(self.info))

        return "\n".join(lines)

    def __iter__(self):
        """Iterate over (q, em_q)."""
        return itertools.izip(self.qpoints, self.emacros_q)

    @property
    def num_qpoints(self):
        return len(self.qpoints)

    @property
    def qfrac_coords(self):
        """The fractional coordinates of the q-points as a ndarray."""
        return self.qpoints.frac_coords

    #@property
    #def has_lfe(self):
    #    """True if MDF includes local field effects."""
    #    return bool(self.info["lfe"])

    #@property
    #def calc_type(self):
    #    """String with the type of calculation."""
    #    return self.info["calc_type"]

    #def show_info(self, stream=sys.stdout):
    #    """Pretty print of the info."""
    #    import pprint
    #    printer = pprint.PrettyPrinter(self, width=80, depth=None, stream=stream)
    #    printer.pprint(self.info)

    @add_fig_kwargs
    def plot(self, ax=None, **kwargs):
        """
        Plot the MDF.

        Args:
            ax: matplotlib `Axes` or None if a new figure should be created.

        ==============  ==============================================================
        kwargs          Meaning
        ==============  ==============================================================
        only_mean       True if only the averaged spectrum is wanted (default True)
        ==============  ==============================================================

        Returns:
            matplotlib figure
        """
        only_mean = kwargs.pop("only_mean", True)

        ax, fig, plt = get_ax_fig_plt(ax)

        ax.grid(True)
        ax.set_xlabel('Frequency [eV]')
        ax.set_ylabel('Macroscopic DF')

        #if not kwargs:
        #    kwargs = {"color": "black", "linewidth": 2.0}

        # Plot the average value
        self.plot_ax(ax, qpoint=None, **kwargs)

        if not only_mean:
            # Plot the q-points
            for iq, qpoint in enumerate(self.qpoints):
                self.plot_ax(ax, iq, **kwargs)

        return fig

    def plot_ax(self, ax, qpoint=None, **kwargs):
        """
        Helper function to plot data on the axis ax.

        Args:
            ax: plot axis.
            qpoint: index of the q-point or Kpoint object or None to plot emacro_avg.
            kwargs: Keyword arguments passed to matplotlib. Accepts also:

                cplx_mode:
                    string defining the data to print (case-insensitive).
                    Possible choices are

                        - "re"  for real part
                        - "im" for imaginary part only.
                        - "abs' for the absolute value

                    Options can be concated with "-".
        """
        # Extract the function to plot according to qpoint.
        if duck.is_intlike(qpoint):
            f = self.emacros_q[int(qpoint)]

        elif isinstance(qpoint, Kpoint):
            iq = self.qpoints.index(qpoint)
            f = self.emacros_q[iq]

        elif qpoint is None:
            f = self.emacro_avg

        else:
            raise ValueError("Don't know how to handle %s" % str(qpoint))

        return f.plot_ax(ax, **kwargs)


class MdfFile(AbinitNcFile, Has_Structure, NotebookWriter):
    """
    Usage example:

    .. code-block:: python

        with MdfFile("foo_MDF.nc") as mdf:
            mdf.plot_mdfs()
    """
    @classmethod
    def from_file(cls, filepath):
        """Initialize the object from a Netcdf file"""
        return cls(filepath)

    def __init__(self, filepath):
        super(MdfFile, self).__init__(filepath)
        self.reader = MdfReader(filepath)

        # TODO Add electron Bands.
        #self._ebands = r.read_ebands()

    def __str__(self):
        """String representation."""
        return self.to_string()

    def to_string(self, verbose=0):
        """String representation."""
        lines = []; app = lines.append

        app(marquee("File Info", mark="="))
        app(self.filestat(as_string=True))
        app("")
        app(self.structure.to_string(title="Structure"))

        app(marquee("Q-points", mark="="))
        app(str(self.qpoints))

        return "\n".join(lines)

    def close(self):
        self.reader.close()

    @lazy_property
    def structure(self):
        """Returns the `Structure` object."""
        return self.reader.read_structure()

    @lazy_property
    def exc_mdf(self):
        "Excitonic macroscopic dieletric function."""
        return self.reader.read_exc_mdf()

    @lazy_property
    def rpanlf_mdf(self):
        """RPA dielectric function without local-field effects."""
        return self.reader.read_rpanlf_mdf()

    @lazy_property
    def gwnlf_mdf(self):
        """RPA-GW dielectric function without local-field effects."""
        return self.reader.read_gwnlf_mdf()

    @property
    def qpoints(self):
        return self.reader.qpoints

    @property
    def qfrac_coords(self):
        """The fractional coordinates of the q-points as a ndarray."""
        return self.qpoints.frac_coords

    @lazy_property
    def params(self):
        """
        Dictionary with the parameters that are usually tested for convergence.
        Used to build Pandas dataframes in Robots.
        """
        return self.reader.read_params()

    def get_mdf(self, mdf_type="exc"):
        """"
        Returns the macroscopic dielectric function.
        """
        return {"exc": self.exc_mdf,
                "rpa": self.rpanlf_mdf,
                "gwrpa": self.gwnlf_mdf}[mdf_type.lower()]

    def plot_mdfs(self, cplx_mode="Im", mdf_type="all", qpoint=None, **kwargs):
        """
        Plot the macroscopic dielectric function.

        Args:
            cplx_mode:
                string defining the data to print (case-insensitive).
                Possible choices are

                    - "re"  for real part
                    - "im" for imaginary part only.
                    - "abs' for the absolute value

                Options can be concated with "-".

            mdf_type:
                Select the type of macroscopic dielectric function.
                Possible choices are

                    - "exc" for the MDF with excitonic effects.
                    - "rpa" for RPA with KS energies.
                    - "gwrpa" for RPA with GW (or KS-corrected) results
                    - "all" if all types are wanted.

                Options can be concated with "-".

            qpoint:
                index of the q-point or Kpoint object or None to plot emacro_avg.
        """
        mdf_type, cplx_mode = mdf_type.lower(), cplx_mode.lower()

        plot_all = mdf_type == "all"
        mdf_type = mdf_type.split("-")

        # Build the plotter.
        plotter = MdfPlotter()

        # Excitonic MDF.
        if "exc" in mdf_type or plot_all:
            plotter.add_mdf("EXC", self.exc_mdf)

        # KS-RPA MDF
        if "rpa" in mdf_type or plot_all:
            plotter.add_mdf("KS-RPA", self.rpanlf_mdf)

        # GW-RPA MDF (obtained with the scissors operator).
        if "gwrpa" in mdf_type or plot_all:
            plotter.add_mdf("GW-RPA", self.gwnlf_mdf)

        # Plot spectra
        return plotter.plot(cplx_mode=cplx_mode, qpoint=qpoint, **kwargs)

    def get_tensor(self, mdf_type="exc"):
        """Get the macroscopic dielectric tensor from the MDF."""
        return DielectricTensor(self.get_mdf(mdf_type), self.structure)

    def write_notebook(self, nbpath=None):
        """
        Write a jupyter notebook to nbpath. If nbpath is None, a temporay file in the current
        working directory is created. Return path to the notebook.
        """
        nbformat, nbv, nb = self.get_nbformat_nbv_nb(title=None)

        nb.cells.extend([
            nbv.new_code_cell("mdf_file = abilab.abiopen('%s')" % self.filepath),
            nbv.new_code_cell("print(mdf_file)"),
            nbv.new_code_cell("mdf_file.plot_mdfs(cplx_mode='Re');"),
            nbv.new_code_cell("mdf_file.plot_mdfs(cplx_mode='Im');"),
            # TODO:
            #nbv.new_code_cell("tensor_exc = mdf_file.get_tensor("exc")")
            #tensor_exc.symmetrize(mdf_file.structure)
            #tensor_exc.plot(title=title)
        ])

        return self._write_nb_nbpath(nb, nbpath)


# TODO Add band energies to MDF file.
#from abipy.electrons import ElectronsReader
class MdfReader(ETSF_Reader): #ElectronsReader
    """
    This object reads data from the MDF.nc file produced by ABINIT.
    """
    def __init__(self, path):
        """Initialize the object from a filename."""
        super(MdfReader, self).__init__(path)
        # Read the structure here to facilitate the creation of the other objects.
        self._structure = self.read_structure()

    @property
    def structure(self):
        return self._structure

    @lazy_property
    def qpoints(self):
        """List of q-points (ndarray)."""
        # Read the fractional coordinates and convert them to KpointList.
        return KpointList(self.structure.reciprocal_lattice, frac_coords=self.read_value("qpoints"))

    @lazy_property
    def wmesh(self):
        """The frequency mesh in eV."""
        return self.read_value("wmesh")

    def read_params(self):
        """Dictionary with the parameters of the run."""
        # TODO: Add more info.
        keys = [
            "nsppol", "ecutwfn", "ecuteps",
            "eps_inf", "soenergy", "broad", "nkibz", "nkbz", "nkibz_interp", "nkbz_interp",
            "wtype", "interp_mode", "nreh", "lomo_spin", "humo_spin"
        ]
        return self.read_keys(keys)

    def _read_mdf(self, mdf_type):
        """Read the MDF from file, returns numpy complex array."""
        return self.read_value(mdf_type, cmode="c")

    def read_exc_mdf(self):
        """Returns the excitonic MDF."""
        info = self.read_params()
        emacros_q = self._read_mdf("exc_mdf")
        return DielectricFunction(self.structure, self.qpoints, self.wmesh, emacros_q, info)

    def read_rpanlf_mdf(self):
        """Returns the KS-RPA MDF without LF effects."""
        info = self.read_params()
        emacros_q = self._read_mdf("rpanlf_mdf")
        return DielectricFunction(self.structure, self.qpoints, self.wmesh, emacros_q, info)

    def read_gwnlf_mdf(self):
        """Returns the GW-RPA MDF without LF effects."""
        info = self.read_params()
        emacros_q = self._read_mdf("gwnlf_mdf")
        return DielectricFunction(self.structure, self.qpoints, self.wmesh, emacros_q, info)


class MdfPlotter(object):
    """
    Class for plotting Macroscopic dielectric functions.

    Usage example:

    .. code-block:: python

        plotter = MdfPlotter()
        plotter.add_mdf("EXC", exc_mdf)
        plotter.add_mdf("KS-RPA", rpanlf_mdf)
        plotter.plot()
    """
    def __init__(self):
        self._mdfs = OrderedDict()

    def add_mdf(self, label, mdf):
        """
        Adds a :class:`DielectricFunction` for plotting.

        Args:
            name: name for the MDF. Must be unique.
            mdf: :class:`DielectricFunction` object.
        """
        if label in self._mdfs:
            raise ValueError("label: %s is already in: %s" % (label, list(self._mdfs.keys())))

        self._mdfs[label] = mdf

    @add_fig_kwargs
    def plot(self, ax=None, cplx_mode="Im", qpoint=None, xlims=None, ylims=None, **kwargs):
        """
        Get a matplotlib plot showing the MDFs.

        Args:
            ax: matplotlib `Axes` or None if a new figure should be created.
            cplx_mode: string defining the data to print (case-insensitive).
                Possible choices are `re` for the real part, `im` for imaginary part only. `abs` for the absolute value.
                Options can be concated with "-".
            qpoint: index of the q-point or :class:`Kpoint` object or None to plot emacro_avg.
            xlims: Set the data limits for the y-axis. Accept tuple e.g. `(left, right)`
                  or scalar e.g. `left`. If left (right) is None, default values are used
            ylims: Same meaning as `ylims` but for the y-axis
        """
        ax, fig, plt = get_ax_fig_plt(ax)
        ax.grid(True)

        ax.set_xlabel('Frequency [eV]')
        ax.set_ylabel('Macroscopic DF')

        cmodes = cplx_mode.split("-")
        qtag = "avg" if qpoint is None else repr(qpoint)

        lines, legends = [], []
        for label, mdf in self._mdfs.items():
            for cmode in cmodes:
                # Plot the average value
                l = mdf.plot_ax(ax, qpoint, cplx_mode=cmode, **kwargs)[0]
                lines.append(l)
                legends.append(r"%s: %s, %s $\varepsilon$" % (cmode, qtag, label))

        # Set legends.
        ax.legend(lines, legends, loc='best', shadow=False)
        set_axlims(ax, xlims, "x")
        set_axlims(ax, ylims, "y")

        return fig

    #def ipw_plot(self)
    #    """
    #    Return an ipython widget with controllers to select the plot.
    #    """
    #    def plot_callback(plot_type, qpoint):
    #        if qpoint == "None": qpoint = None
    #        return self.plot(cplx_type=cplx_type, qpoint=qpoint)

    #    import ipywidgets as ipw
    #    return ipw.interact_manual(
    #            plot_callback,
    #            cplx_type=["re", "im", "abs"],
    #            qpoint=["None"] + list(range(self.,
    #        )



class MultipleMdfPlotter(object):
    """
    Class for plotting multipe macroscopic dielectric functions
    extracted from several MDF.nc files

    Usage example:

    .. code-block:: python

        plotter = MultipleMdfPlotter()
        plotter.add_mdf_file("file1", mdf_file1)
        plotter.add_mdf_file("file2", mdf_file2)
        plotter.plot()
    """
    # By default the plotter will extracts these MDF types.
    MDF_TYPES = ("exc", "rpa", "gwrpa")

    # Mapping mdf_type --> color used in plots.
    #MDF_TYPE2COLOR = {"exc": "red", "rpa": "blue", "gwrpa": "yellow"}

    #MDF_TYPE2LINESTYLE = {"exc": "red", "rpa": "blue", "gwrpa": "yellow"}

    # Mapping [mdf_type][cplx_mode] --> ylable used in plots.
    MDF_TYPECPLX2TEX = {
        "exc": dict(re=r"$\Re(\varepsilon_{exc})$", im=r"$\Im(\varepsilon_{exc}$)", abs=r"$|\varepsilon_{exc}|$"),
        "rpa": dict(re=r"$\Re(\varepsilon_{rpa})$", im=r"$\Im(\varepsilon_{rpa})$", abs=r"$|\varepsilon_{rpa}|$"),
        "gwrpa": dict(re=r"$\Re(\varepsilon_{gw-rpa})$", im=r"$\Im(\varepsilon_{gw-rpa})$", abs= r"$|\varepsilon_{gw-rpa}|$"),
        }

    #alpha = 0.6

    def __init__(self):
        # [label][mdf_type] --> DielectricFunction
        self._mdfs = OrderedDict()

    def __str__(self):
        return self.to_string()

    def to_string(self, **kwargs):
        """String representation."""
        lines = []
        app = lines.append

        for label, mdf_dict in self._mdfs.items():
            app(marquee(label, mark="="))
            for mdf_type, mdf in mdf_dict.items():
                app("%s: %s" % (mdf_type, mdf.to_string(**kwargs)))

        return "\n".join(lines)

    def add_mdf_file(self, label, obj):
        """
        Extract dielectric functions from `obj`, store data for plotting.

        Args:
            label: label associated to the MDF file. Must be unique.
            mdf: filepath or :class:`MdfFile` object.
        """
        if label in self._mdfs:
            raise ValueError("label: %s already in: %s" % (label, list(self._mdfs.keys())))

        self._mdfs[label] = OrderedDict()

        if is_string(obj):
            # Open the file.
            with MdfFile(obj) as mdf_file:
                for mdf_type in self.MDF_TYPES:
                    self._mdfs[label][mdf_type] = mdf_file.get_mdf(mdf_type=mdf_type)
        else:
            # Extract data from `MdfFile` object
            for mdf_type in self.MDF_TYPES:
                self._mdfs[label][mdf_type] = obj.get_mdf(mdf_type=mdf_type)

    @add_fig_kwargs
    def plot(self, mdf_type="exc", qview="avg", xlims=None, ylims=None, **kwargs):
        """
        Plot all macroscopic dielectric functions (MDF) stored in the plotter

        Args:
            mdf_type: Selects the type of dielectric function.
                "exc" for the MDF with excitonic effects.
                "rpa" for RPA with KS energies.
                "gwrpa" for RPA with GW (or KS-corrected) results.
            qview: "avg" to plot the results averaged over q-points. "all" to plot q-point dependence.
            xlims: Set the data limits for the y-axis. Accept tuple e.g. `(left, right)`
                  or scalar e.g. `left`. If left (right) is None, default values are used
            ylims: Same meaning as `ylims` but for the y-axis

        Return: matplotlib figure
        """
        # Build plot grid.
        if qview == "avg":
            ncols, nrows = 2, 1
        elif qview == "all":
            qpoints = self._get_qpoints()
            ncols, nrows = 2, len(qpoints)
        else:
            raise ValueError("Invalid value of qview: %s" % str(qview))

        import matplotlib.pyplot as plt
        fig, axmat = plt.subplots(nrows=nrows, ncols=ncols, sharex=True, sharey=True, squeeze=False)

        if qview == "avg":
            # Plot averaged values
            self.plot_mdftype_cplx(mdf_type, "Re", ax=axmat[0, 0], xlims=xlims, ylims=ylims,
                                   with_legend=True, show=False)
            self.plot_mdftype_cplx(mdf_type, "Im", ax=axmat[0, 1], xlims=xlims, ylims=ylims,
                                   with_legend=False, show=False)
        elif qview == "all":
            # Plot MDF(q)
            nqpt = len(qpoints)
            for iq, qpt in enumerate(qpoints):
                islast = (iq == nqpt - 1)
                self.plot_mdftype_cplx(mdf_type, "Re", qpoint=qpt, ax=axmat[iq, 0], xlims=xlims, ylims=ylims,
                                       with_legend=(iq == 0), with_xlabel=islast, with_ylabel=islast, show=False)
                self.plot_mdftype_cplx(mdf_type, "Im", qpoint=qpt, ax=axmat[iq, 1], xlims=xlims, ylims=ylims,
                                       with_legend=False, with_xlabel=islast, with_ylabel=islast, show=False)

        else:
            raise ValueError("Invalid value of qview: %s" % str(qview))

        #axmat[0, 0].legend(loc="best")
        #fig.tight_layout()

        return fig

    #@add_fig_kwargs
    #def plot_mdftypes(self, qview="avg", xlims=None, ylims=None, **kwargs):
    #    """

    #    Args:
    #        qview:
    #        xlims
    #        ylims

    #    Return: matplotlib figure
    #    """
    #    # Build plot grid.
    #    if qview == "avg":
    #        ncols, nrows = 2, 1
    #    elif qview == "all":
    #        qpoints = self._get_qpoints()
    #        ncols, nrows = 2, len(qpoints)
    #    else:
    #        raise ValueError("Invalid value of qview: %s" % str(qview))

    #    import matplotlib.pyplot as plt
    #    fig, axmat = plt.subplots(nrows=nrows, ncols=ncols, sharex=True, sharey=True, squeeze=False)

    #    if qview == "avg":
    #        # Plot averaged values
    #        for mdf_type in self.MDF_TYPES:
    #            self.plot_mdftype_cplx(mdf_type, "Re", ax=axmat[0, 0], xlims=xlims, ylims=ylims,
    #                                   with_legend=True, show=False)
    #            self.plot_mdftype_cplx(mdf_type, "Im", ax=axmat[0, 1], xlims=xlims, ylims=ylims,
    #                                   with_legend=False, show=False)
    #    elif qview == "all":
    #        # Plot MDF(q)
    #        nqpt = len(qpoints)
    #        for iq, qpt in enumerate(qpoints):
    #            islast = (iq == nqpt - 1)
    #            for mdf_type in self.MDF_TYPES:
    #                self.plot_mdftype_cplx(mdf_type, "Re", qpoint=qpt, ax=axmat[iq, 0], xlims=xlims, ylims=ylims,
    #                                       with_legend=(iq == 0), with_xlabel=islast, with_ylabel=islast, show=False)
    #                self.plot_mdftype_cplx(mdf_type, "Im", qpoint=qpt, ax=axmat[iq, 1], xlims=xlims, ylims=ylims,
    #                                       with_legend=False, with_xlabel=islast, with_ylabel=islast, show=False)

    #    else:
    #        raise ValueError("Invalid value of qview: %s" % str(qview))

    #    #axmat[0, 0].legend(loc="best")
    #    #fig.tight_layout()

    #    return fig

    @add_fig_kwargs
    def plot_mdftype_cplx(self, mdf_type, cplx_mode, qpoint=None, ax=None,
                          xlims=None, ylims=None, with_legend=True, with_xlabel=True, with_ylabel=True, **kwargs):
        """
        Helper function to plot data corresponds to `mdf_type`, `cplx_mode`, `qpoint`.

        Args:
            ax: matplotlib `Axes` or None if a new figure should be created.
            mdf_type:
            cplx_mode: string defining the data to print (case-insensitive).
                Possible choices are `re` for the real part, `im` for imaginary part only. `abs` for the absolute value.
            qpoint: index of the q-point or :class:`Kpoint` object or None to plot emacro_avg.
            xlims: Set the data limits for the y-axis. Accept tuple e.g. `(left, right)`
                  or scalar e.g. `left`. If left (right) is None, default values are used
            ylims: Same meaning as `ylims` but for the y-axis
            with_legend: True if legend should be added
            with_xlabel:
            with_ylabel:

        Return: matplotlib figure
        """
        ax, fig, plt = get_ax_fig_plt(ax)
        ax.grid(True)

        if with_xlabel: ax.set_xlabel(r'$\omega [eV]$')
        if with_ylabel: ax.set_ylabel(self.MDF_TYPECPLX2TEX[mdf_type][cplx_mode.lower()])

        can_use_basename = self._can_use_basenames_as_labels()
        qtag = "avg" if qpoint is None else repr(qpoint)

        lines, legends = [], []
        for label, mdf_dict in self._mdfs.items():
            mdf = mdf_dict[mdf_type]
            # Plot the average value
            l = mdf.plot_ax(ax, qpoint, cplx_mode=cplx_mode, **kwargs)[0]
            lines.append(l)
            if can_use_basename:
                label = os.path.basename(label)
            else:
                # Use relative paths if label is a file.
                if os.path.isfile(label): label = os.path.relpath(label)

            legends.append(r"%s: %s, %s $\varepsilon$" % (cplx_mode, qtag, label))

        set_axlims(ax, xlims, "x")
        set_axlims(ax, ylims, "y")

        # Set legends.
        if with_legend:
            ax.legend(lines, legends, loc='best', shadow=False)

        return fig

    def _get_qpoints(self):
        """
        This function is called when we have to plot quantities as function of q-points.
        It checks that all dielectric functions stored in the plotter have the same list of
        q-points and returns the q-points of the first dielectric function.

        Raises: ValueError if the q-points cannot be compared.
        """
        qpoints, errors = [], []
        eapp = errors.append
        for i, d in enumerate(self._mdfs.values()):
            mdf = d[self.MDF_TYPES[0]]
            if i == 0:
                qpoints = mdf.qpoints
            else:
                if qpoints != mdf.qpoints:
                    eapp("List of q-points for MDF index %i does not agree with first set:\n" % str(qpoints))

        if errors:
            msg = "\n".join(errors)
            raise ValueError(msg + "\n" +
                             "Your MDF files have been computed with a different set of q-points\n" +
                             "Cannot compare dielectric functions as as function of q, use average value")

        return qpoints

    def ipw_select_plot(self):
        """
        Return an ipython widget with controllers to select the plot.
        """
        def plot_callback(mdf_type, qview):
            return self.plot(mdf_type=mdf_type, qview=qview)

        import ipywidgets as ipw
        return ipw.interact_manual(
                plot_callback,
                mdf_type=["exc", "rpa", "gwrpa"],
                qview=["avg", "all"],
            )

    def _can_use_basenames_as_labels(self):
        """
        Return True if all labels represent valid files and the basenames are unique
        In this case one can use the file basename instead of the full path in the plots.
        """
        if not all(os.path.exists(l) for l in self._mdfs): return False
        labels = [os.path.basename(l) for l in self._mdfs]
        return len(set(labels)) == len(labels)


class MdfRobot(Robot, RobotWithEbands):
    """
    This robot analyzes the results contained in multiple MDF files.
    """
    EXT = "MDF"

    def get_multimdf_plotter(self, cls=None):
        """
        Return an instance of MultipleMdfPlotter to compare multiple dielectric functions.
        """
        from abipy.electrons.bse import MultipleMdfPlotter
        plotter = MultipleMdfPlotter() if cls is None else cls()

        for label, mdf in self:
            plotter.add_mdf_file(label, mdf)

        return plotter

    def get_dataframe(self, with_geo=False, abspath=False, funcs=None, **kwargs):
        """
        Build and return Pandas dataframe with the most import BSE results.
        and the filenames as index.

        Args:
            with_geo: True if structure info should be added to the dataframe
            abspath: True if paths in index should be absolute. Default: Relative to getcwd().
            funcs: Function or list of functions to execute to add more data to the DataFrame.
                Each function receives a :class:`MdfFile` object and returns a tuple (key, value)
                where key is a string with the name of column and value is the value to be inserted.

        Return:
            pandas DataFrame
        """
        rows, row_names = [], []
        for i, (label, mdf) in enumerate(self):
            row_names.append(label)
            d = OrderedDict([
                ("exc_mdf", mdf.exc_mdf),
                ("rpa_mdf", mdf.rpanlf_mdf),
                ("gwrpa_mdf", mdf.gwnlf_mdf),
            ])
            #d = {aname: getattr(mdf, aname) for aname in attrs}
            #d.update({"qpgap": mdf.get_qpgap(spin, kpoint)})

            # Add convergence parameters
            d.update(mdf.params)

            # Add info on structure.
            if with_geo:
                d.update(mdf.structure.get_dict4frame(with_spglib=True))

            # Execute functions.
            if funcs is not None: d.update(self._exec_funcs(funcs, mdf))
            rows.append(d)

        row_names = row_names if not abspath else self._to_relpaths(row_names)
        return pd.DataFrame(rows, index=row_names, columns=list(rows[0].keys()))

    #@add_fig_kwargs
    #def plot_conv_mdf(self, hue, mdf_type="exc_mdf", **kwargs):
    #    import matplotlib.pyplot as plt
    #    frame = self.get_dataframe()
    #    grouped = frame.groupby(hue)

    #    fig, ax_list = plt.subplots(nrows=len(grouped), ncols=1, sharex=True, sharey=True, squeeze=True)

    #    for i, (hue_val, group) in enumerate(grouped):
    #        #print(group)
    #        mdfs = group[mdf_type]
    #        ax = ax_list[i]
    #        ax.set_title("%s = %s" % (hue, hue_val))
    #        for mdf in mdfs:
    #            mdf.plot_ax(ax)

    #    return fig

    def write_notebook(self, nbpath=None):
        """
        Write a jupyter notebook to nbpath. If nbpath is None, a temporay file in the current
        working directory is created. Return path to the notebook.
        """
        nbformat, nbv, nb = self.get_nbformat_nbv_nb(title=None)

        args = [(l, f.filepath) for l, f in self.items()]
        nb.cells.extend([
            #nbv.new_markdown_cell("# This is a markdown cell"),
            nbv.new_code_cell("robot = abilab.MdfRobot(*%s)\nrobot.trim_paths()\nrobot" % str(args)),
            nbv.new_code_cell("#df = robot.get_dataframe(with_geo=False"),
            nbv.new_code_cell("plotter = robot.get_multimdf_plotter()"),
            nbv.new_code_cell('plotter.plot(mdf_type="exc", qview="avg", xlim=None, ylim=None);'),
            #nbv.new_code_cell(plotter.combiboxplot();"),
        ])

        # Mixins
        nb.cells.extend(self.get_baserobot_code_cells())
        nb.cells.extend(self.get_ebands_code_cells())

        return self._write_nb_nbpath(nb, nbpath)
