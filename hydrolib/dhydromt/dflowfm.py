"""Implement plugin model class"""

import glob
from os.path import join, basename, isfile
import logging

import pandas as pd
from rasterio.warp import transform_bounds
import pyproj
import geopandas as gpd
from shapely.geometry import box
import xarray as xr

import hydromt
from hydromt.models.model_api import Model
from hydromt import gis_utils, io
from hydromt import raster

from .workflows import process_branches
from .workflows import validate_branches
from .workflows import generate_roughness
from .workflows import generate_crosssections

from .workflows import helper
from . import DATADIR

import hydrolib.dhydromt.workflows.setup_functions as delft3dfmpy_setupfuncs

# TODO: replace all functions with delft3dfmpy_setupfuncs prefix

from pathlib import Path

__all__ = ["DFlowFMModel"]
logger = logging.getLogger(__name__)


class DFlowFMModel(Model):
    """General and basic API for models in HydroMT"""

    # FIXME
    _NAME = "dflowfm"
    _CONF = "FMmdu.txt"
    _DATADIR = DATADIR
    # TODO change below mapping table (hydrolib-core convention:shape file convention) to be read from data folder, maybe similar to _intbl for wflow
    # TODO: we also need one reverse table to read from static geom back. maybe a dictionary of data frame is better?
    # TODO: write static geom as geojson dataset, so that we dont get limitation for the 10 characters
    _GEOMS = {
        "region": {},
        "branches": {
            "branchId": "BR_ID",
            "branchType": "BR_TYPE",
        },
        "branch_nodes": {},
        "roughness": {
            "frictionId": "FR_ID",
            "frictionValue": "FR_VAL",
            "frictionType": "FR_TYPE",
        },
        "crsdefs": {
            "id": "CRS_DEFID",
            "type": "CRS_TYPE",
            "thalweg": "CRS_THAL",
            "height": "CRS_H",
            "width": "CRS_W",
            "t_WIDTH": "CRS_TW",
            "closed": "CRS_CL",
            "diameter": "CRS_D",
            "frictionId": "FR_ID",
            "frictionValue": "FR_VAL",
            "frictionType": "FR_TYPE",
        },
        "crslocs": {
            "id": "CRS_LOCID",
            "branchId": "BR_ID",
            "chainage": "CRS_CHAI",
            "shift": "CRS_SHIF",
            "definition": "CRS_DEFID",
            "frictionValue": "FR_VAL",
            "frictionType": "FR_TYPE",
        },
    }  # FIXME Mapping from hydromt names to model specific names
    _MAPS = {}  # FIXME Mapping from hydromt names to model specific names
    _FOLDERS = ["dflowfm", "staticgeoms"]

    def __init__(
        self,
        root=None,
        mode="w",
        config_fn=None,  # hydromt config contain glob section, anything needed can be added here as args
        data_libs=None,  # yml # TODO: how to choose global mapping files (.csv) and project specific mapping files (.csv)
        logger=logger,
        deltares_data=False,  # data from pdrive,
    ):

        if not isinstance(root, (str, Path)):
            raise ValueError("The 'root' parameter should be a of str or Path.")

        super().__init__(
            root=root,
            mode=mode,
            config_fn=config_fn,
            data_libs=data_libs,
            deltares_data=deltares_data,
            logger=logger,
        )

        # model specific
        # default ini files
        self._region_name = "DFlowFM"
        self._ini_settings = (
            None,
        )  # TODO: add all ini files in one object? e.g. self._intbl in wflow?
        self._datamodel = None
        self._dfmmodel = None  # TODO: replace with hydrolib-core object

        # TODO: assign hydrolib-core components

    def setup_basemaps(
        self,
        region,
        region_name=None,
        **kwargs,
    ):
        """Define the model region.
        Adds model layer:
        * **region** geom: model region
        Parameters
        ----------
        region: dict
            Dictionary describing region of interest, e.g. {'bbox': [xmin, ymin, xmax, ymax]}.
            See :py:meth:`~hydromt.workflows.parse_region()` for all options.
        """

        kind, region = hydromt.workflows.parse_region(region, logger=self.logger)
        if kind == "bbox":
            geom = gpd.GeoDataFrame(geometry=[box(*region["bbox"])], crs=4326)
        elif kind == "grid":
            geom = region["grid"].raster.box
        elif kind == "geom":
            geom = region["geom"]
        else:
            raise ValueError(
                f"Unknown region kind {kind} for DFlowFM, expected one of ['bbox', 'grid', 'geom']."
            )

        if geom.crs is None:
            raise AttributeError("region crs can not be None. ")
        else:
            self.logger.info(f"Model region is set to crs: {geom.crs.to_epsg()}")

        # Set the model region geometry (to be accessed through the shortcut self.region).
        self.set_staticgeoms(geom, "region")

        if region_name is not None:
            self._region_name = region_name

        # FIXME: how to deprecate WARNING:root:No staticmaps defined

    def _get_geodataframe(
        self,
        path_or_key: str,
        id_col: str,
        clip_buffer: float = 0,  # TODO: think about whether to keep/remove, maybe good to put into the ini file.
        clip_predicate: str = "contains",
        **kwargs,
    ) -> gpd.GeoDataFrame:
        """Function to get geodataframe.

        This function combines a wrapper around :py:meth:`~hydromt.data_adapter.DataCatalog.get_geodataset`

        Arguments
        ---------
        path_or_key: str
            Data catalog key. If a path to a vector file is provided it will be added
            to the data_catalog with its based on the file basename without extension.

        Returns
        -------
        gdf: geopandas.GeoDataFrame
            GeoDataFrame

        """

        # pop kwargs from catalogue
        d = self.data_catalog.to_dict(path_or_key)[path_or_key].pop("kwargs")

        # set clipping
        clip_buffer = d.get("clip_buffer", clip_buffer)
        clip_predicate = d.get("clip_predicate", clip_predicate)  # TODO: in ini file

        # read data + clip data + preprocessing data
        df = self.data_catalog.get_geodataframe(
            path_or_key,
            geom=self.region,
            buffer=clip_buffer,
            clip_predicate=clip_predicate,
        )
        self.logger.debug(
            f"GeoDataFrame: {len(df)} feature are read after clipping region with clip_buffer = {clip_buffer}, clip_predicate = {clip_predicate}"
        )

        # retype data
        retype = d.get("retype", None)
        df = helper.retype_geodataframe(df, retype)

        # eval funcs on data
        funcs = d.get("funcs", None)
        df = helper.eval_funcs(df, funcs)

        # slice data # TODO: test what can be achived by the alias in yml file
        required_columns = d.get("required_columns", None)
        required_query = d.get("required_query", None)
        df = helper.slice_geodataframe(
            df, required_columns=required_columns, required_query=required_query
        )
        self.logger.debug(
            f"GeoDataFrame: {len(df)} feature are sliced after applying required_columns = {required_columns}, required_query = '{required_query}'"
        )

        # index data
        if id_col is None:
            pass
        elif id_col not in df.columns:
            raise ValueError(
                f"GeoDataFrame: cannot index data using id_col = {id_col}. id_col must exist in data columns ({df.columns})"
            )
        else:
            self.logger.debug(f"GeoDataFrame: indexing with id_col: {id_col}")
            df.index = df[id_col]
            df.index.name = id_col

        # remove nan in id
        df_na = df.index.isna()
        if len(df_na) > 0:
            df = df[~df_na]
            self.logger.debug(f"GeoDataFrame: removing index with NaN")

        # remove duplicated
        df_dp = df.duplicated()
        if len(df_dp) > 0:
            df = df.drop_duplicates()
            self.logger.debug(f"GeoDataFrame: removing duplicates")

        # report
        df_num = len(df)
        if df_num == 0:
            self.logger.warning(f"Zero features are read from {path_or_key}")
        else:
            self.logger.info(f"{len(df)} features read from {path_or_key}")

        return df

    def setup_branches(
        self,
        branches_fn: str,
        branches_ini_fn: str = None,
        snap_offset: float = 0.0,
        id_col: str = "branchId",
        branch_query: str = None,
        pipe_query: str = 'branchType == "Channel"',  # TODO update to just TRUE or FALSE keywords instead of full query
        channel_query: str = 'branchType == "Pipe"',
        **kwargs,
    ):
        """This component prepares the 1D branches

        Adds model layers:

        * **branches** geom: 1D branches vector

        Parameters
        ----------
        branches_fn : str
            Name of data source for branches parameters, see data/data_sources.yml.

            * Required variables: branchId, branchType, # TODO: now still requires some cross section stuff

            * Optional variables: []

        """
        self.logger.info(f"Preparing 1D branches.")

        # initialise data model
        branches_ini = helper.parse_ini(
            Path(self._DATADIR).joinpath("dflowfm", f"branch_settings.ini")
        )  # TODO: make this default file complete, need 2 more argument, spacing? yes/no, has_in_branch crosssection? yes/no, or maybe move branches, cross sections and roughness into basemap
        branches = None
        branch_nodes = None
        # TODO: initilise hydrolib-core object --> build
        # TODO: call hydrolib-core object --> update

        # read branch_ini
        if branches_ini_fn is None or not branches_ini_fn.is_file():
            self.logger.warning(
                f"branches_ini_fn ({branches_ini_fn}) does not exist. Fall back choice to defaults. "
            )
            branches_ini_fn = Path(self._DATADIR).joinpath(
                "dflowfm", f"branch_settings.ini"
            )

        branches_ini.update(helper.parse_ini(branches_ini_fn))
        self.logger.info(f"branch default settings read from {branches_ini_fn}.")

        # read branches
        if branches_fn is None:
            raise ValueError("branches_fn must be specified.")

        branches = self._get_geodataframe(branches_fn, id_col=id_col, **kwargs)
        self.logger.info(f"branches read from {branches_fn} in Data Catalogue.")

        branches = helper.append_data_columns_based_on_ini_query(branches, branches_ini)

        # select branches to use
        if helper.check_geodataframe(branches) and branch_query is not None:
            branches = branches.query(branch_query)
            self.logger.info(f"Query branches for {branch_query}")

        # process branches and generate branch_nodes
        if helper.check_geodataframe(branches):
            self.logger.info(f"Processing branches")
            branches, branch_nodes = process_branches(
                branches,
                branch_nodes,
                branches_ini=branches_ini,  # TODO:  make the branch_setting.ini [global] functions visible in the setup functions. Use kwargs to allow user interaction. Make decisions on what is neccessary and what not
                id_col=id_col,
                snap_offset=snap_offset,
                logger=self.logger,
            )

        # validate branches
        # TODO: integrate below into validate module
        if helper.check_geodataframe(branches):
            self.logger.info(f"Validating branches")
            validate_branches(branches)

        # finalise branches
        # setup channels
        branches.loc[branches.query(channel_query).index, "branchType"] = "Channel"
        # setup pipes
        branches.loc[branches.query(pipe_query).index, "branchType"] = "Pipe"
        # assign crs
        branches.crs = self.crs

        # setup staticgeoms
        self.logger.debug(f"Adding branches and branch_nodes vector to staticgeoms.")
        self.set_staticgeoms(branches, "branches")
        self.set_staticgeoms(branch_nodes, "branch_nodes")

        # TODO: assign hydrolib-core object

        return branches, branch_nodes

    def setup_roughness(
        self,
        generate_roughness_from_branches: bool = True,
        roughness_ini_fn: str = None,
        branch_query: str = None,
        **kwargs,
    ):
        """"""

        self.logger.info(f"Preparing 1D roughness.")

        # initialise ini settings and data
        roughness_ini = helper.parse_ini(
            Path(self._DATADIR).joinpath("dflowfm", f"roughness_settings.ini")
        )
        # TODO: how to make sure if defaults are given, we can also know it based on friction name or cross section definiation id (can we use an additional column for that? )
        roughness = None

        # TODO: initilise hydrolib-core object --> build
        # TODO: call hydrolib-core object --> update

        # update ini settings
        if roughness_ini_fn is None or not roughness_ini_fn.is_file():
            self.logger.warning(
                f"roughness_ini_fn ({roughness_ini_fn}) does not exist. Fall back choice to defaults. "
            )
            roughness_ini_fn = Path(self._DATADIR).joinpath(
                "dflowfm", f"roughness_settings.ini"
            )

        roughness_ini.update(helper.parse_ini(roughness_ini_fn))
        self.logger.info(f"roughness default settings read from {roughness_ini}.")

        if generate_roughness_from_branches == True:

            self.logger.info(f"Generating roughness from branches. ")

            # update data by reading user input
            _branches = self.staticgeoms["branches"]

            # update data by combining ini settings
            self.logger.debug(
                f'1D roughness initialised with the following attributes: {list(roughness_ini.get("default", {}))}.'
            )
            branches = helper.append_data_columns_based_on_ini_query(
                _branches, roughness_ini
            )

            # select branches to use e.g. to facilitate setup a selection each time setup_* is called
            if branch_query is not None:
                branches = branches.query(branch_query)
                self.logger.info(f"Query branches for {branch_query}")

            # process data
            if helper.check_geodataframe(branches):
                roughness, branches = generate_roughness(branches, roughness_ini)

            # add staticgeoms
            if helper.check_geodataframe(roughness):
                self.logger.debug(f"Updating branches vector to staticgeoms.")
                self.set_staticgeoms(branches, "branches")

                self.logger.debug(f"Updating roughness vector to staticgeoms.")
                self.set_staticgeoms(roughness, "roughness")

            # TODO: add hydrolib-core object

        else:

            # TODO: setup roughness from other data types

            pass

    def setup_crosssections(
        self,
        generate_crosssections_from_branches: bool = True,
        crosssections_ini_fn: str = None,
        branch_query: str = None,
        **kwargs,
    ):
        """"""

        self.logger.info(f"Preparing 1D crosssections.")

        # initialise ini settings and data
        crosssections_ini = helper.parse_ini(
            Path(self._DATADIR).joinpath("dflowfm", f"crosssection_settings.ini")
        )
        crsdefs = None
        crslocs = None
        # TODO: initilise hydrolib-core object --> build
        # TODO: call hydrolib-core object --> update

        # update ini settings
        if crosssections_ini_fn is None or not crosssections_ini_fn.is_file():
            self.logger.warning(
                f"crosssection_ini_fn ({crosssections_ini_fn}) does not exist. Fall back choice to defaults. "
            )
            crosssections_ini_fn = Path(self._DATADIR).joinpath(
                "dflowfm", f"crosssection_settings.ini"
            )

        crosssections_ini.update(helper.parse_ini(crosssections_ini_fn))
        self.logger.info(
            f"crosssections default settings read from {crosssections_ini}."
        )

        if generate_crosssections_from_branches == True:

            # set crosssections from branches (1D)
            self.logger.info(f"Generating 1D crosssections from 1D branches.")

            # update data by reading user input
            _branches = self.staticgeoms["branches"]

            # update data by combining ini settings
            self.logger.debug(
                f'1D crosssections initialised with the following attributes: {list(crosssections_ini.get("default", {}))}.'
            )
            branches = helper.append_data_columns_based_on_ini_query(
                _branches, crosssections_ini
            )

            # select branches to use e.g. to facilitate setup a selection each time setup_* is called
            if branch_query is not None:
                branches = branches.query(branch_query)
                self.logger.info(f"Query branches for {branch_query}")

            if helper.check_geodataframe(branches):
                crsdefs, crslocs, branches = generate_crosssections(
                    branches, crosssections_ini
                )

            # update new branches with crsdef info to staticgeoms
            self.logger.debug(f"Updating branches vector to staticgeoms.")
            self.set_staticgeoms(branches, "branches")

            # add new crsdefs to staticgeoms
            self.logger.debug(f"Adding crsdefs vector to staticgeoms.")
            self.set_staticgeoms(
                gpd.GeoDataFrame(
                    crsdefs,
                    geometry=gpd.points_from_xy([0] * len(crsdefs), [0] * len(crsdefs)),
                ),
                "crsdefs",
            )  # FIXME: make crsdefs a vector to be add to static geoms. using dummy locations --> might cause issue for structures

            # add new crslocs to staticgeoms
            self.logger.debug(f"Adding crslocs vector to staticgeoms.")
            self.set_staticgeoms(crslocs, "crslocs")

        else:

            # TODO: setup roughness from other data types, e.g. points, xyz

            pass
            # raise NotImplementedError()

    def setup_manholes(
        self,
        manholes_ini_fn: str = None,
        manholes_fn: str = None,
        id_col: str = None,
        snap_offset: float = 1,
        rename_map: dict = None,
        required_columns: list = None,
        required_dtypes: list = None,
        logger=logging,
    ):
        """"""

        self.logger.info(f"Preparing manholes.")
        _branches = self.staticgeoms["branches"]

        # Setup of branches and manholes
        manholes, branches = delft3dfmpy_setupfuncs.setup_manholes(
            _branches,
            manholes_fn=delft3dfmpy_setupfuncs.parse_arg(
                manholes_fn
            ),  # FIXME: hydromt config parser could not parse '' to None
            manholes_ini_fn=delft3dfmpy_setupfuncs.parse_arg(
                manholes_ini_fn
            ),  # FIXME: hydromt config parser could not parse '' to None
            snap_offset=snap_offset,
            id_col=id_col,
            rename_map=delft3dfmpy_setupfuncs.parse_arg(
                rename_map
            ),  # TODO: replace with data adaptor
            required_columns=delft3dfmpy_setupfuncs.parse_arg(
                required_columns
            ),  # TODO: replace with data adaptor
            required_dtypes=delft3dfmpy_setupfuncs.parse_arg(
                required_dtypes
            ),  # TODO: replace with data adaptor
            logger=logger,
        )

        self.logger.debug(f"Adding manholes vector to staticgeoms.")
        self.set_staticgeoms(manholes, "manholes")

        self.logger.debug(f"Updating branches vector to staticgeoms.")
        self.set_staticgeoms(branches, "branches")

    def setup_bridges(
        self,
        roughness_ini_fn: str = None,
        bridges_ini_fn: str = None,
        bridges_fn: str = None,
        id_col: str = None,
        branch_query: str = None,
        snap_method: str = "overall",
        snap_offset: float = 1,
        rename_map: dict = None,
        required_columns: list = None,
        required_dtypes: list = None,
        logger=logging,
    ):
        """"""

        self.logger.info(f"Preparing bridges.")
        _branches = self.staticgeoms["branches"]
        _crsdefs = self.staticgeoms["crsdefs"]
        _crslocs = self.staticgeoms["crslocs"]

        bridges, crsdefs = delft3dfmpy_setupfuncs.setup_bridges(
            _branches,
            _crsdefs,
            _crslocs,
            delft3dfmpy_setupfuncs.parse_arg(roughness_ini_fn),
            delft3dfmpy_setupfuncs.parse_arg(bridges_ini_fn),
            delft3dfmpy_setupfuncs.parse_arg(bridges_fn),
            id_col,
            delft3dfmpy_setupfuncs.parse_arg(
                branch_query
            ),  # TODO: replace with data adaptor
            snap_method,
            snap_offset,
            delft3dfmpy_setupfuncs.parse_arg(
                rename_map
            ),  # TODO: replace with data adaptor
            delft3dfmpy_setupfuncs.parse_arg(
                required_columns
            ),  # TODO: replace with data adaptor
            delft3dfmpy_setupfuncs.parse_arg(
                required_dtypes
            ),  # TODO: replace with data adaptor
            logger,
        )

        self.logger.debug(f"Adding bridges vector to staticgeoms.")
        self.set_staticgeoms(bridges, "bridges")

        self.logger.debug(f"Updating crsdefs vector to staticgeoms.")
        self.set_staticgeoms(crsdefs, "crsdefs")

    def setup_gates(
        self,
        roughness_ini_fn: str = None,
        gates_ini_fn: str = None,
        gates_fn: str = None,
        id_col: str = None,
        branch_query: str = None,
        snap_method: str = "overall",
        snap_offset: float = 1,
        rename_map: dict = None,
        required_columns: list = None,
        required_dtypes: list = None,
        logger=logging,
    ):
        """"""

        self.logger.info(f"Preparing gates.")
        _branches = self.staticgeoms["branches"]
        _crsdefs = self.staticgeoms["crsdefs"]
        _crslocs = self.staticgeoms["crslocs"]

        gates = delft3dfmpy_setupfuncs.setup_gates(
            _branches,
            _crsdefs,
            _crslocs,
            delft3dfmpy_setupfuncs.parse_arg(roughness_ini_fn),
            delft3dfmpy_setupfuncs.parse_arg(gates_ini_fn),
            delft3dfmpy_setupfuncs.parse_arg(gates_fn),
            id_col,
            delft3dfmpy_setupfuncs.parse_arg(
                branch_query
            ),  # TODO: replace with data adaptor
            snap_method,
            snap_offset,
            delft3dfmpy_setupfuncs.parse_arg(
                rename_map
            ),  # TODO: replace with data adaptor
            delft3dfmpy_setupfuncs.parse_arg(
                required_columns
            ),  # TODO: replace with data adaptor
            delft3dfmpy_setupfuncs.parse_arg(
                required_dtypes
            ),  # TODO: replace with data adaptor
            logger,
        )

        self.logger.debug(f"Adding gates vector to staticgeoms.")
        self.set_staticgeoms(gates, "gates")

    def setup_pumps(
        self,
        roughness_ini_fn: str = None,
        pumps_ini_fn: str = None,
        pumps_fn: str = None,
        id_col: str = None,
        branch_query: str = None,
        snap_method: str = "overall",
        snap_offset: float = 1,
        rename_map: dict = None,
        required_columns: list = None,
        required_dtypes: list = None,
        logger=logging,
    ):
        """"""

        self.logger.info(f"Preparing gates.")
        _branches = self.staticgeoms["branches"]
        _crsdefs = self.staticgeoms["crsdefs"]
        _crslocs = self.staticgeoms["crslocs"]

        pumps = delft3dfmpy_setupfuncs.setup_pumps(
            _branches,
            _crsdefs,
            _crslocs,
            delft3dfmpy_setupfuncs.parse_arg(roughness_ini_fn),
            delft3dfmpy_setupfuncs.parse_arg(pumps_ini_fn),
            delft3dfmpy_setupfuncs.parse_arg(pumps_fn),
            id_col,
            delft3dfmpy_setupfuncs.parse_arg(
                branch_query
            ),  # TODO: replace with data adaptor
            snap_method,
            snap_offset,
            delft3dfmpy_setupfuncs.parse_arg(
                rename_map
            ),  # TODO: replace with data adaptor
            delft3dfmpy_setupfuncs.parse_arg(
                required_columns
            ),  # TODO: replace with data adaptor
            delft3dfmpy_setupfuncs.parse_arg(
                required_dtypes
            ),  # TODO: replace with data adaptor
            logger,
        )

        self.logger.debug(f"Adding pumps vector to staticgeoms.")
        self.set_staticgeoms(pumps, "pumps")

    def setup_culverts(
        self,
        roughness_ini_fn: str = None,
        culverts_ini_fn: str = None,
        culverts_fn: str = None,
        id_col: str = None,
        branch_query: str = None,
        snap_method: str = "overall",
        snap_offset: float = 1,
        rename_map: dict = None,
        required_columns: list = None,
        required_dtypes: list = None,
        logger=logging,
    ):
        """"""

        self.logger.info(f"Preparing culverts.")
        _branches = self.staticgeoms["branches"]
        _crsdefs = self.staticgeoms["crsdefs"]
        _crslocs = self.staticgeoms["crslocs"]

        culverts, crsdefs = delft3dfmpy_setupfuncs.setup_culverts(
            _branches,
            _crsdefs,
            _crslocs,
            delft3dfmpy_setupfuncs.parse_arg(roughness_ini_fn),
            delft3dfmpy_setupfuncs.parse_arg(culverts_ini_fn),
            delft3dfmpy_setupfuncs.parse_arg(culverts_fn),
            id_col,
            delft3dfmpy_setupfuncs.parse_arg(
                branch_query
            ),  # TODO: replace with data adaptor
            snap_method,
            snap_offset,
            delft3dfmpy_setupfuncs.parse_arg(
                rename_map
            ),  # TODO: replace with data adaptor
            delft3dfmpy_setupfuncs.parse_arg(
                required_columns
            ),  # TODO: replace with data adaptor
            delft3dfmpy_setupfuncs.parse_arg(
                required_dtypes
            ),  # TODO: replace with data adaptor
            logger,
        )

        self.logger.debug(f"Adding culverts vector to staticgeoms.")
        self.set_staticgeoms(culverts, "culverts")

        self.logger.debug(f"Updating crsdefs vector to staticgeoms.")
        self.set_staticgeoms(crsdefs, "crsdefs")

    def setup_compounds(
        self,
        roughness_ini_fn: str = None,
        compounds_ini_fn: str = None,
        compounds_fn: str = None,
        id_col: str = None,
        branch_query: str = None,
        snap_method: str = "overall",
        snap_offset: float = 1,
        rename_map: dict = None,
        required_columns: list = None,
        required_dtypes: list = None,
        logger=logging,
    ):
        """"""

        self.logger.info(f"Preparing compounds.")
        _structures = [
            self.staticgeoms[s]
            for s in ["bridges", "gates", "pumps", "culverts"]
            if s in self.staticgeoms.keys()
        ]

        compounds = delft3dfmpy_setupfuncs.setup_compounds(
            _structures,
            delft3dfmpy_setupfuncs.parse_arg(roughness_ini_fn),
            delft3dfmpy_setupfuncs.parse_arg(compounds_ini_fn),
            delft3dfmpy_setupfuncs.parse_arg(compounds_fn),
            id_col,
            delft3dfmpy_setupfuncs.parse_arg(
                branch_query
            ),  # TODO: replace with data adaptor
            snap_method,
            snap_offset,
            delft3dfmpy_setupfuncs.parse_arg(
                rename_map
            ),  # TODO: replace with data adaptor
            delft3dfmpy_setupfuncs.parse_arg(
                required_columns
            ),  # TODO: replace with data adaptor
            delft3dfmpy_setupfuncs.parse_arg(
                required_dtypes
            ),  # TODO: replace with data adaptor
            logger,
        )

        self.logger.debug(f"Adding compounds vector to staticgeoms.")
        self.set_staticgeoms(compounds, "compounds")

    def setup_boundaries(
        self,
        boundaries_fn: str = None,
        boundaries_fn_ini: str = None,
        id_col: str = None,
        rename_map: dict = None,
        required_columns: list = None,
        required_dtypes: list = None,
        logger=logging,
    ):
        """"""

        self.logger.info(f"Preparing boundaries.")
        _structures = [
            self.staticgeoms[s]
            for s in ["bridges", "gates", "pumps", "culverts"]
            if s in self.staticgeoms.keys()
        ]

        boundaries = delft3dfmpy_setupfuncs.setup_boundaries(
            delft3dfmpy_setupfuncs.parse_arg(boundaries_fn),
            delft3dfmpy_setupfuncs.parse_arg(boundaries_fn_ini),
            id_col,
            delft3dfmpy_setupfuncs.parse_arg(
                rename_map
            ),  # TODO: replace with data adaptor
            delft3dfmpy_setupfuncs.parse_arg(
                required_columns
            ),  # TODO: replace with data adaptor
            delft3dfmpy_setupfuncs.parse_arg(
                required_dtypes
            ),  # TODO: replace with data adaptor
            logger,
        )

        self.logger.debug(f"Adding boundaries vector to staticgeoms.")
        self.set_staticgeoms(boundaries, "boundaries")

    def _setup_datamodel(self):

        """setup data model using dfm and drr naming conventions"""
        if self._datamodel == None:
            self._datamodel = delft3dfmpy_setupfuncs.setup_dm(
                self.staticgeoms, logger=self.logger
            )

    def setup_dflowfm(
        self,
        model_type: str = "1d",
        one_d_mesh_distance: float = 40,
    ):
        """ """
        self.logger.info(f"Preparing DFlowFM 1D model.")

        self._setup_datamodel()

        self._dfmmodel = delft3dfmpy_setupfuncs.setup_dflowfm(
            self._datamodel,
            model_type=model_type,
            one_d_mesh_distance=one_d_mesh_distance,
            logger=self.logger,
        )

    ## I/O

    def read(self):
        """Method to read the complete model schematization and configuration from file."""
        self.logger.info(f"Reading model data from {self.root}")
        self.read_config()
        self.read_staticmaps()
        self.read_staticgeoms()
        self.read_dfmmodel()

    def write(self):  # complete model
        """Method to write the complete model schematization and configuration to file."""
        self.logger.info(f"Writing model data to {self.root}")
        # if in r, r+ mode, only write updated components
        if not self._write:
            self.logger.warning("Cannot write in read-only mode")
            return
        if self.config:  # try to read default if not yet set
            self.write_config()  # FIXME: config now isread from default, modified and saved temporaryly in the models folder --> being read by dfm and modify?
        if self._staticmaps:
            self.write_staticmaps()
        if self._staticgeoms:
            self.write_staticgeoms()
        if self._dfmmodel:
            self.write_dfmmodel()
        if self._forcing:
            self.write_forcing()

    def read_staticmaps(self):
        """Read staticmaps at <root/?/> and parse to xarray Dataset"""
        # to read gdal raster files use: hydromt.open_mfraster()
        # to read netcdf use: xarray.open_dataset()
        if not self._write:
            # start fresh in read-only mode
            self._staticmaps = xr.Dataset()
        self.set_staticmaps(hydromt.open_mfraster(join(self.root, "*.tif")))

    def write_staticmaps(self):
        """Write staticmaps at <root/?/> in model ready format"""
        # to write to gdal raster files use: self.staticmaps.raster.to_mapstack()
        # to write to netcdf use: self.staticmaps.to_netcdf()
        if not self._write:
            raise IOError("Model opened in read-only mode")
        self.staticmaps.raster.to_mapstack(join(self.root, "dflowfm"))

    def read_staticgeoms(self):
        """Read staticgeoms at <root/?/> and parse to dict of geopandas"""
        if not self._write:
            # start fresh in read-only mode
            self._staticgeoms = dict()
        for fn in glob.glob(join(self.root, "*.xy")):
            name = basename(fn).replace(".xy", "")
            geom = hydromt.open_vector(fn, driver="xy", crs=self.crs)
            self.set_staticgeoms(geom, name)

    def write_staticgeoms(self):  # write_all()
        """Write staticmaps at <root/?/> in model ready format"""
        # TODO: write_data_catalogue with updates of the rename based on mapping table?
        if not self._write:
            raise IOError("Model opened in read-only mode")
        for name, gdf in self.staticgeoms.items():
            fn_out = join(self.root, "staticgeoms", f"{name}.shp")
            helper.write_shp(
                self.staticgeoms[name].rename(columns=self._GEOMS[name]), fn_out
            )

    def read_forcing(self):
        """Read forcing at <root/?/> and parse to dict of xr.DataArray"""
        return self._forcing
        # raise NotImplementedError()

    def write_forcing(self):
        """write forcing at <root/?/> in model ready format"""
        pass
        # raise NotImplementedError()

    def read_dfmmodel(self):
        """Read dfmmodel at <root/?/> and parse to model class (deflt3dfmpy)"""
        pass
        # raise NotImplementedError()

    def write_dfmmodel(self):
        """Write dfmmodel at <root/?/> in model ready format"""
        if not self._write:
            raise IOError("Model opened in read-only mode")
        delft3dfmpy_setupfuncs.write_dfmmodel(
            self.dfmmodel, output_dir=self.root, name="DFLOWFM", logger=self.logger
        )

    def read_states(self):
        """Read states at <root/?/> and parse to dict of xr.DataArray"""
        return self._states
        # raise NotImplementedError()

    def write_states(self):
        """write states at <root/?/> in model ready format"""
        pass
        # raise NotImplementedError()

    def read_results(self):
        """Read results at <root/?/> and parse to dict of xr.DataArray"""
        return self._results
        # raise NotImplementedError()

    def write_results(self):
        """write results at <root/?/> in model ready format"""
        pass
        # raise NotImplementedError()

    @property
    def crs(self):
        return pyproj.CRS.from_epsg(self.get_config("global.epsg", fallback=4326))

    @property
    def region_name(self):
        return self._region_name

    @property
    def dfmmodel(self):
        if self._dfmmodel == None:
            self.init_dfmmodel()
        return self._dfmmodel

    def init_dfmmodel(self):
        self._dfmmodel = delft3dfmpy_setupfuncs.DFlowFMModel()
