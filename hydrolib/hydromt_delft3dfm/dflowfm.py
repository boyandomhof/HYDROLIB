"""Implement plugin model class"""

import glob
import logging
from os.path import basename, isfile, join
from pathlib import Path
from typing import Union

import geopandas as gpd
import hydromt
import numpy as np
import pandas as pd
import pyproj
import xarray as xr
from hydromt import gis_utils, io, raster
from hydromt.models.model_api import Model
from rasterio.warp import transform_bounds
from shapely.geometry import box

from hydrolib.core.io.crosssection.models import *
from hydrolib.core.io.friction.models import *
from hydrolib.core.io.mdu.models import FMModel
from hydrolib.core.io.net.models import *
from hydrolib.dhydamo.geometry import common, mesh, viz

from . import DATADIR
from .workflows import (
    generate_roughness,
    helper,
    process_branches,
    set_branch_crosssections,
    set_xyz_crosssections,
    update_data_columns_attribute_from_query,
    update_data_columns_attributes,
    validate_branches,
    invert_levels_from_dem,
)

__all__ = ["DFlowFMModel"]
logger = logging.getLogger(__name__)


class DFlowFMModel(Model):
    """General and basic API for models in HydroMT"""

    # FIXME
    _NAME = "dflowfm"
    _CONF = "DFlowFM.mdu"
    _DATADIR = DATADIR
    # TODO change below mapping table (hydrolib-core convention:shape file convention) to be read from data folder, maybe similar to _intbl for wflow
    # TODO: we also need one reverse table to read from static geom back. maybe a dictionary of data frame is better?
    # TODO: write static geom as geojson dataset, so that we dont get limitation for the 10 characters
    _GEOMS = {}  # FIXME Mapping from hydromt names to model specific names
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
        self._branches = None
        self._config_fn = (
            join("dflowfm", self._CONF) if config_fn is None else config_fn
        )
        self.write_config()  #  create the mdu file in order to initialise dfmmodedl properly and at correct output location
        self._dfmmodel = self.init_dfmmodel()

    def setup_basemaps(
        self,
        region: dict,
        crs: int = None,
    ):
        """Define the model region.

        Adds model layer:

        * **region** geom: model region

        Parameters
        ----------
        region: dict
            Dictionary describing region of interest, e.g. {'bbox': [xmin, ymin, xmax, ymax]}.
            See :py:meth:`~hydromt.workflows.parse_region()` for all options.
        crs : int, optional
            Coordinate system (EPSG number) of the model. If not provided, equal to the region crs
            if "grid" or "geom" option are used, and to 4326 if "bbox" is used.
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

        if crs:
            geom = geom.to_crs(crs)
        elif geom.crs is None:
            raise AttributeError("region crs can not be None. ")
        else:
            self.logger.info(f"Model region is set to crs: {geom.crs.to_epsg()}")

        # Set the model region geometry (to be accessed through the shortcut self.region).
        self.set_staticgeoms(geom, "region")

        # FIXME: how to deprecate WARNING:root:No staticmaps defined

    def _setup_branches(
        self,
        gdf_br: gpd.GeoDataFrame,
        defaults: pd.DataFrame,
        br_type: str,
        spacing: pd.DataFrame = None,
        snap_offset: float = 0.0,
        allow_intersection_snapping: bool = True,
    ):
        """This function is a wrapper for all common steps to add branches type of objects (ie channels, rivers, pipes...).

        Parameters
        ----------
        gdf_br : gpd.GeoDataFrame
            GeoDataFrame with the new branches to add.
        spacing : pd.DataFrame
            DataFrame containing spacing values per 'branchType', 'shape', 'width' or 'diameter'.

        """
        if gdf_br.crs.is_geographic:  # needed for length and splitting
            gdf_br = gdf_br.to_crs(3857)

        self.logger.info("Adding/Filling branches attributes values")
        gdf_br = update_data_columns_attributes(gdf_br, defaults, brtype=br_type)

        # If specific spacing info from spacing_fn, update spacing attribute
        if spacing is not None:
            self.logger.info(f"Updating spacing attributes")
            gdf_br = update_data_columns_attribute_from_query(
                gdf_br, spacing, attribute_name="spacing"
            )
        # Line smoothing for pipes
        smooth_branches = False
        if br_type == "pipe":
            smooth_branches = True

        self.logger.info(f"Processing branches")
        branches, branches_nodes = process_branches(
            gdf_br,
            branch_nodes=None,
            id_col="branchId",
            snap_offset=snap_offset,
            allow_intersection_snapping=allow_intersection_snapping,
            smooth_branches=smooth_branches,
            logger=self.logger,
        )

        self.logger.info(f"Validating branches")
        validate_branches(branches)

        # convert to model crs
        branches = branches.to_crs(self.crs)
        branches_nodes = branches_nodes.to_crs(self.crs)

        return branches, branches_nodes

    def setup_channels(
        self,
        channels_fn: str,
        channels_defaults_fn: str = None,
        spacing_fn: str = None,
        snap_offset: float = 0.0,
        allow_intersection_snapping: bool = True,
    ):
        """This component prepares the 1D channels and adds to branches 1D network

        Adds model layers:

        * **channels** geom: 1D channels vector
        * **branches** geom: 1D branches vector

        Parameters
        ----------
        channels_fn : str
            Name of data source for branches parameters, see data/data_sources.yml.

            * Required variables: [branchId, branchType] # TODO: now still requires some cross section stuff

            * Optional variables: [spacing, material, shape, diameter, width, t_width, t_width_up, width_up,
              width_dn, t_width_dn, height, height_up, height_dn, inlev_up, inlev_dn, bedlev_up, bedlev_dn,
              closed, manhole_up, manhole_dn]
        channels_defaults_fn : str Path
            Path to a csv file containing all defaults values per 'branchType'.
        spacing : str Path
            Path to a csv file containing spacing values per 'branchType', 'shape', 'width' or 'diameter'.

        """
        self.logger.info(f"Preparing 1D channels.")

        # Read the channels data
        id_col = "branchId"
        gdf_ch = self.data_catalog.get_geodataframe(
            channels_fn, geom=self.region, buffer=10, predicate="contains"
        )
        gdf_ch.index = gdf_ch[id_col]
        gdf_ch.index.name = id_col

        if gdf_ch.index.size == 0:
            self.logger.warning(
                f"No {channels_fn} 1D channel locations found within domain"
            )
            return None

        else:
            # Fill in with default attributes values
            if channels_defaults_fn is None or not channels_defaults_fn.is_file():
                self.logger.warning(
                    f"channels_defaults_fn ({channels_defaults_fn}) does not exist. Fall back choice to defaults. "
                )
                channels_defaults_fn = Path(self._DATADIR).joinpath(
                    "channels", "channels_defaults.csv"
                )
            defaults = pd.read_csv(channels_defaults_fn)
            self.logger.info(
                f"channel default settings read from {channels_defaults_fn}."
            )

            # If specific spacing info from spacing_fn, update spacing attribute
            spacing = None
            if isinstance(spacing_fn, str):
                if not isfile(spacing_fn):
                    self.logger.error(f"Spacing file not found: {spacing_fn}, skipping")
                else:
                    spacing = pd.read_csv(spacing_fn)

            # Build the channels branches and nodes and fill with attributes and spacing
            channels, channel_nodes = self._setup_branches(
                gdf_br=gdf_ch,
                defaults=defaults,
                br_type="channel",
                spacing=spacing,
                snap_offset=snap_offset,
                allow_intersection_snapping=allow_intersection_snapping,
            )

            # setup staticgeoms #TODO do we still need channels?
            self.logger.debug(
                f"Adding branches and branch_nodes vector to staticgeoms."
            )
            self.set_staticgeoms(channels, "channels")
            self.set_staticgeoms(channel_nodes, "channel_nodes")

            # add to branches
            self.add_branches(channels, branchtype="channel")

    def setup_rivers(
        self,
        rivers_fn: str,
        rivers_defaults_fn: str = None,
        river_filter: str = None,
        friction_type: str = "Manning",  # what about constructing friction_defaults_fn?
        friction_value: float = 0.023,
        crosssections_fn: str = None,
        crosssections_type: str = None,
        snap_offset: float = 0.0,
        allow_intersection_snapping: bool = True,
    ):
        """Prepares the 1D rivers and adds to 1D branches.

        1D rivers must contain valid geometry, friction and crosssections.

        The river geometry is read from ``rivers_fn``. If defaults attributes
        [branchOrder, spacing, material, shape, width, t_width, height, bedlev, closed] are not present in ``rivers_fn``,
        they are added from defaults values in ``rivers_defaults_fn``. For branchId and branchType, they are created on the fly
        if not available in rivers_fn ("river" for Type and "river_{i}" for Id).

        Friction attributes are either taken from ``rivers_fn`` or filled in using ``friction_type`` and
        ``friction_value`` arguments.
        Note for now only branch friction or global friction is supported.

        Crosssections are read from ``crosssections_fn`` based on the ``crosssections_type``. If there is no
        ``crosssections_fn`` values are derived at the centroid of each river line based on defaults.

        Adds/Updates model layers:

        * **rivers** geom: 1D rivers vector
        * **branches** geom: 1D branches vector
        * **crosssections** geom: 1D crosssection vector

        Parameters
        ----------
        rivers_fn : str
            Name of data source for rivers parameters, see data/data_sources.yml.
            Note only the lines that are within the region polygon + 10m buffer will be used.
            * Optional variables: [branchId, branchType, branchOrder, material, friction_type, friction_value]
        rivers_defaults_fn : str Path
            Path to a csv file containing all defaults values per 'branchType'.
        river_filter: str, optional
            Keyword in branchType column of rivers_fn used to filter river lines. If None all lines in rivers_fn are used (default).
        friction_type : str, optional
            Type of friction tu use. One of ["Manning", "Chezy", "wallLawNikuradse", "WhiteColebrook", "StricklerNikuradse", "Strickler", "deBosBijkerk"].
            By default "Manning".
        friction_value : float, optional.
            Units corresponding to [friction_type] are ["Chézy C [m 1/2 /s]", "Manning n [s/m 1/3 ]", "Nikuradse k_n [m]", "Nikuradse k_n [m]", "Nikuradse k_n [m]", "Strickler k_s [m 1/3 /s]", "De Bos-Bijkerk γ [1/s]"]
            Friction value. By default 0.023.
        crosssections_fn : str Path, optional
            Name of data source for crosssections, see data/data_sources.yml.
            If ``crosssections_type`` = "xyzpoints"
            * Required variables: [crsId, order, z]
            If ``crosssections_type`` = "points"
            * Required variables: [crsId, order, z]
            By default None, crosssections will be set from branches
        crosssections_type : str, optional
            Type of crosssections read from crosssections_fn. One of ["xyzpoints"].
            By default None.
        snap_offset: float, optional
            Snapping tolerance to automatically connecting branches.
            By default 0.0, no snapping is applied.
        allow_intersection_snapping: bool, optional
            Switch to choose whether snapping of multiple branch ends are allowed when ``snap_offset`` is used.
            By default True.

        See Also
        ----------
        dflowfm._setup_branches
        """
        self.logger.info(f"Preparing 1D rivers.")

        # Read the rivers data
        gdf_riv = self.data_catalog.get_geodataframe(
            rivers_fn, geom=self.region, buffer=10, predicate="contains"
        )
        # Filter features based on river_filter
        if river_filter is not None and "branchType" in gdf_riv.columns:
            gdf_riv = gdf_riv[gdf_riv["branchType"] == river_filter]
        # Check if features in region
        if len(gdf_riv) == 0:
            self.logger.warning(
                f"No {rivers_fn} 1D river locations found within domain"
            )
            return None

        # Add branchType and branchId attributes if does not exist
        if "branchType" not in gdf_riv.columns:
            gdf_riv["branchType"] = pd.Series(
                data=np.repeat("river", len(gdf_riv)), index=gdf_riv.index, dtype=str
            )
        if "branchId" not in gdf_riv.columns:
            data = [f"river_{i}" for i in np.arange(1, len(gdf_riv) + 1)]
            gdf_riv["branchId"] = pd.Series(data, index=gdf_riv.index, dtype=str)

        # assign id
        id_col = "branchId"
        gdf_riv.index = gdf_riv[id_col]
        gdf_riv.index.name = id_col

        # assign default attributes
        if rivers_defaults_fn is None or not rivers_defaults_fn.is_file():
            self.logger.warning(
                f"rivers_defaults_fn ({rivers_defaults_fn}) does not exist. Fall back choice to defaults. "
            )
            rivers_defaults_fn = Path(self._DATADIR).joinpath(
                "rivers", "rivers_defaults.csv"
            )
        defaults = pd.read_csv(rivers_defaults_fn)
        self.logger.info(f"river default settings read from {rivers_defaults_fn}.")

        # filter for allowed columns
        _allowed_columns = [
            "geometry",
            "branchId",
            "branchType",
            "branchOrder",
            "material",
            "shape",
            "diameter",
            "width",
            "t_width",
            "height",
            "bedlev",
            "closed",
            "friction_type",
            "friction_value",
        ]
        allowed_columns = set(_allowed_columns).intersection(gdf_riv.columns)
        gdf_riv = gpd.GeoDataFrame(gdf_riv[allowed_columns], crs=gdf_riv.crs)

        # Add friction to defaults
        defaults["frictionType"] = friction_type
        defaults["frictionValue"] = friction_value

        # Build the rivers branches and nodes and fill with attributes and spacing
        rivers, river_nodes = self._setup_branches(
            gdf_br=gdf_riv,
            defaults=defaults,
            br_type="river",
            spacing=None,  # does not allow spacing for rivers
            snap_offset=snap_offset,
            allow_intersection_snapping=allow_intersection_snapping,
        )

        # Add friction_id column based on {friction_type}_{friction_value}
        rivers["frictionId"] = [
            f"{ftype}_{fvalue}"
            for ftype, fvalue in zip(rivers["frictionType"], rivers["frictionValue"])
        ]

        # setup crosssections
        if crosssections_type is None:
            crosssections_type = "branch"  # TODO: maybe assign a specific one for river, like branch_river
        assert {crosssections_type}.issubset({"xyzpoints", "branch"})
        crosssections = self._setup_crosssections(
            branches=rivers,
            crosssections_fn=crosssections_fn,
            crosssections_type=crosssections_type,
        )

        # setup staticgeoms #TODO do we still need channels?
        self.logger.debug(f"Adding rivers and river_nodes vector to staticgeoms.")
        self.set_staticgeoms(rivers, "rivers")
        self.set_staticgeoms(river_nodes, "rivers_nodes")

        # add to branches
        self.add_branches(rivers, branchtype="river")

    def setup_pipes(
        self,
        pipes_fn: str,
        pipes_defaults_fn: Union[str, None] = None,
        pipe_filter: Union[str, None] = None,
        spacing: float = 50,
        friction_type: str = "WhiteColebrook",
        friction_value: float = 0.003,
        crosssections_shape: str = "circle",
        crosssections_value: Union[int, list] = 0.5,
        dem_fn: Union[str, None] = None,
        pipes_depth: int = -2.0,
        pipes_invlev: int = -2.5,
        snap_offset: float = 0.0,
        allow_intersection_snapping: bool = True,
    ):
        """Prepares the 1D pipes and adds to 1D branches.

        1D pipes must contain valid geometry, friction, crosssections and up- and downstream invert levels.

        The pipe geometry is read from ``pipes_fn``. If defaults attributes
        [branchOrder, spacing, material] are not present in ``pipes_fn``,
        they are added from defaults values in ``pipes_defaults_fn``. For branchId and branchType, they are created on the fly
        if not available in pipes_fn ("pipe" for Type and "pipe_{i}" for Id).

        Friction attributes are either taken from ``pipes_fn`` or filled in using ``friction_type`` and
        ``friction_value`` arguments.
        Note for now only branch friction or global friction is supported.

        Crosssections attributes are either taken from ``pipes_fn`` attributes ["shape", "diameter", "width", "height"]
        or filled in using ``crosssections_shape`` and ``crosssections_value``.

        Up- and downstream invert levels are either taken from ``pipes_fn`` ["invlev_up", "invlev_dn"], or derived from ``dem_fn``
        minus a fixed depth ``pipe_depth`` [m], or from a constant ``pipe_invlev`` [m asl] (not recommended! should be edited before a model run).

        Adds/Updates model layers:

        * **rivers** geom: 1D rivers vector
        * **branches** geom: 1D branches vector
        * **crosssections** geom: 1D crosssection vector

        Parameters
        ----------
        pipes_fn : str
            Name of data source for pipes parameters, see data/data_sources.yml.
            Note only the lines that are within the region polygon + 10m buffer will be used.
            * Optional variables: [branchId, branchType, branchOrder, spacing, material, friction_type, friction_value, shape, diameter, width, height, invlev_up, invlev_dn]
        pipes_defaults_fn : str Path
            Path to a csv file containing all defaults values per 'branchType'.
        pipe_filter: str, optional
            Keyword in branchType column of pipes_fn used to filter pipe lines. If None all lines in pipes_fn are used (default).
        spacing, float, optional
            Spacing value in meters to split the pipes lines into smaller computation units. By default 50 meters.
        friction_type : str, optional
            Type of friction tu use. One of ["Manning", "Chezy", "wallLawNikuradse", "WhiteColebrook", "StricklerNikuradse", "Strickler", "deBosBijkerk"].
            By default "WhiteColeBrook".
        friction_value : float, optional.
            Units corresponding to [friction_type] are ["Chézy C [m 1/2 /s]", "Manning n [s/m 1/3 ]", "Nikuradse k_n [m]", "Nikuradse k_n [m]", "Nikuradse k_n [m]", "Strickler k_s [m 1/3 /s]", "De Bos-Bijkerk γ [1/s]"]
            Friction value. By default 0.003.
        crosssections_shape : str, optional
            Shape of pipe crosssections. Either "circle" (default) or "rectangle".
        crosssections_value : int or list of int, optional
            Crosssections parameter value.
            If ``crosssections_shape`` = "circle", expects a diameter (default with 0.5 m) [m]
            If ``crosssections_shape`` = "rectangle", expects a list with [width, height] (e.g. [1.0, 1.0]) [m]
        dem_fn: str, optional
            Name of data source for dem data. Used to derive default invert levels values (DEM - pipes_depth - pipes diameter/height).
            * Required variables: [elevtn]
        pipes_depth: int, optional
            Depth of the pipes in the ground [m] (default -2.0 m). Used to derive defaults invert levels values (DEM - pipes_depth - pipes diameter/height).
        pipes_invlev: int, optional
            Constant default invert levels of the pipes [m asl] (default -2.5 m asl). This is not a recommended method.
        snap_offset: float, optional
            Snapping tolenrance to automatically connecting branches.
            By default 0.0, no snapping is applied.
        allow_intersection_snapping: bool, optional
            Switch to choose whether snapping of multiple branch ends are allowed when ``snap_offset`` is used.
            By default True.

        See Also
        ----------
        dflowfm._setup_branches
        """
        self.logger.info(f"Preparing 1D pipes.")

        # Read the pipes data
        gdf_pipe = self.data_catalog.get_geodataframe(
            pipes_fn, geom=self.region, predicate="contains"
        )
        # Filter features based on pipe_filter
        if pipe_filter is not None and "branchType" in gdf_pipe.columns:
            gdf_pipe = gdf_pipe[gdf_pipe["branchType"] == pipe_filter]
        # Check if features in region
        if len(gdf_pipe) == 0:
            self.logger.warning(f"No {pipes_fn} pipe locations found within domain")
            return None

        # Add branchType and branchId attributes if does not exist
        if "branchType" not in gdf_pipe.columns:
            gdf_pipe["branchType"] = pd.Series(
                data=np.repeat("pipe", len(gdf_pipe)), index=gdf_pipe.index, dtype=str
            )
        if "branchId" not in gdf_pipe.columns:
            data = [f"pipe_{i}" for i in np.arange(1, len(gdf_pipe) + 1)]
            gdf_pipe["branchId"] = pd.Series(data, index=gdf_pipe.index, dtype=str)

        # assign id
        id_col = "branchId"
        gdf_pipe.index = gdf_pipe[id_col]
        gdf_pipe.index.name = id_col

        # assign default attributes
        if pipes_defaults_fn is None or not pipes_defaults_fn.is_file():
            self.logger.warning(
                f"pipes_defaults_fn ({pipes_defaults_fn}) does not exist. Fall back choice to defaults. "
            )
            pipes_defaults_fn = Path(self._DATADIR).joinpath(
                "pipes", "pipes_defaults.csv"
            )
        defaults = pd.read_csv(pipes_defaults_fn)
        self.logger.info(f"pipe default settings read from {pipes_defaults_fn}.")

        # filter for allowed columns
        _allowed_columns = [
            "geometry",
            "branchId",
            "branchType",
            "branchOrder",
            "material",
            "spacing",
            "shape",  # circle or rectangle
            "diameter",  # circle
            "width",  # rectangle
            "height",  # rectangle
            "invlev_up",
            "inlev_dn",
        ]
        allowed_columns = set(_allowed_columns).intersection(gdf_pipe.columns)
        gdf_pipe = gpd.GeoDataFrame(gdf_pipe[allowed_columns], crs=gdf_pipe.crs)

        # Add spacing and friction to defaults
        defaults["spacing"] = spacing
        defaults["frictionType"] = friction_type
        defaults["frictionValue"] = friction_value

        # Add crosssections to defaults
        if crosssections_shape == "circle":
            if isinstance(crosssections_value, float):
                defaults["shape"] = crosssections_shape
                defaults["diameter"] = crosssections_value
            else:
                # TODO: warning or error?
                self.logger.warning(
                    "If crosssections_shape is circle, crosssections_value should be a single float for diameter. Skipping setup_pipes."
                )
                return
        elif crosssections_shape == "rectangle":
            if isinstance(crosssections_value, list) and len(crosssections_value) == 2:
                defaults["shape"] = crosssections_shape
                defaults["width"], defaults["height"] = crosssections_value
            else:
                # TODO: warning or error?
                self.logger.warning(
                    "If crosssections_shape is rectangle, crosssections_value should be a list with [width, height] values. Skipping setup_pipes."
                )
                return
        else:
            self.logger.warning(
                f"crosssections_shape {crosssections_shape} argument not understood. Should be one of [circle, rectangle]. Skipping setup_pipes"
            )
            return

        # Build the rivers branches and nodes and fill with attributes and spacing
        pipes, pipe_nodes = self._setup_branches(
            gdf_br=gdf_pipe,
            defaults=defaults,
            br_type="pipe",
            spacing=None,  # for now only single default value implemented
            snap_offset=snap_offset,
            allow_intersection_snapping=allow_intersection_snapping,
        )

        # Add friction_id column based on {friction_type}_{friction_value}
        pipes["frictionId"] = [
            f"{ftype}_{fvalue}"
            for ftype, fvalue in zip(pipes["frictionType"], pipes["frictionValue"])
        ]

        # setup invert levels
        # 1. check if invlev up and dn are fully filled in (nothing needs to be done)
        if "invlev_up" and "invlev_dn" in pipes.columns:
            inv = pipes[["invlev_up", "invlev_dn"]]
            if inv.isnull().sum().sum() > 0:  # nodata values in pipes for invert levels
                fill_invlev = True
                self.logger.info(
                    f"{pipes_fn} data has {inv.isnull().sum().sum()} no data values for invert levels. Will be filled using dem_fn or default value {pipes_invlev}"
                )
            else:
                fill_invlev = False
        else:
            fill_invlev = True
            self.logger.info(
                f"{pipes_fn} does not have columns [invlev_up, invlev_dn]. INvert levels will be generated from dem_fn or default value {pipes_invlev}"
            )
        # 2. else use dem_fn + pipe_depth
        if fill_invlev and dem_fn is not None:
            dem = self.data_catalog.get_rasterdataset(
                dem_fn, geom=self.region, variables=["elevtn"]
            )
            pipes = invert_levels_from_dem(gdf=pipes, dem=dem, depth=pipes_depth)
        # 3. else use default value
        else:
            self.logger.warning(
                "!Using a constant up and down invert levels for all pipes. May cause issues when running the delft3dfm model.!"
            )
            df_inv = pd.DataFrame(
                data={
                    "branchType": ["pipe"],
                    "invlev_up": [pipes_invlev],
                    "invlev_dn": [pipes_invlev],
                }
            )
            pipes = update_data_columns_attributes(pipes, df_inv, brtype="pipe")

        # TODO: check that geometry lines are properly oriented from up to dn when deriving invert levels from dem

        # Update crosssections object
        self._setup_crosssections(pipes, crosssections_type="branch", midpoint=False)

        # setup staticgeoms
        self.logger.debug(f"Adding pipes and pipe_nodes vector to staticgeoms.")
        self.set_staticgeoms(pipes, "pipes")
        self.set_staticgeoms(pipe_nodes, "pipe_nodes")

        # add to branches
        self.add_branches(pipes, branchtype="pipe", node_distance=np.inf)

    def _setup_crosssections(
        self,
        branches,
        crosssections_fn: str = None,
        crosssections_type: str = "branch",
        midpoint=True,
    ):
        """Prepares 1D crosssections.
        crosssections can be set from branchs, xyzpoints, # TODO to be extended also from dem data for rivers/channels?
        Crosssection must only be used after friction has been setup.

        Crosssections are read from ``crosssections_fn``.
        Crosssection types of this file is read from ``crosssections_type``
        If ``crosssections_type`` = "xyzpoints":
            * Required variables: crsId, order, z
            * Optional variables:

        If ``crosssections_fn`` is not defined, default method is ``crosssections_type`` = 'branch',
        meaning that branch attributes will be used to derive regular crosssections.
        Crosssections are derived at branches mid points if ``midpoints`` is True,
        else at both upstream and downstream extremities of branches if False.

        Adds/Updates model layers:
        * **crosssections** geom: 1D crosssection vector

        Parameters
        ----------
        branches : gpd.GeoDataFrame
            geodataframe of the branches to apply crosssections.
            * Required variables: [branchId, branchType, branchOrder]
            * Optional variables: [material, friction_type, friction_value]
        crosssections_fn : str Path, optional
            Name of data source for crosssections, see data/data_sources.yml.
            If ``crosssections_type`` = "xyzpoints"
            Note that only points within the region + 1000m buffer will be read.
            * Required variables: crsId, order, z
            * Optional variables:
            If ``crosssections_type`` = "points"
            * Required variables: crsId, order, z
            * Optional variables:
            By default None, crosssections will be set from branches
        crosssections_type : str, optional
            Type of crosssections read from crosssections_fn. One of ["xyzpoints"].
            By default None.
        """

        # setup crosssections
        self.logger.info(f"Preparing 1D crosssections.")
        # if 'crosssections' in self.staticgeoms.keys():
        #    crosssections = self._staticgeoms['crosssections']
        # else:
        #    crosssections = gpd.GeoDataFrame()

        # TODO: allow multiple crosssection filenamess

        if crosssections_fn is None and crosssections_type == "branch":
            # TODO: set a seperate type for rivers because other branch types might require upstream/downstream

            # read crosssection from branches
            gdf_cs = set_branch_crosssections(branches, midpoint=midpoint)

        elif crosssections_type == "xyz":

            # Read the crosssection data
            gdf_cs = self.data_catalog.get_geodataframe(
                crosssections_fn,
                geom=self.region,
                buffer=1000,
                predicate="contains",
            )

            # check if feature valid
            if len(gdf_cs) == 0:
                self.logger.warning(
                    f"No {crosssections_fn} 1D xyz crosssections found within domain"
                )
                return None
            valid_attributes = helper.heck_gpd_attributes(
                gdf_cs, required_columns=["crsId", "order", "z"]
            )
            if not valid_attributes:
                self.logger.error(
                    f"Required attributes [crsId, order, z] in xyz crosssections do not exist"
                )
                return None

            # assign id
            id_col = "crsId"
            gdf_cs.index = gdf_cs[id_col]
            gdf_cs.index.name = id_col

            # reproject to model crs
            gdf_cs.to_crs(self.crs)

            # set crsloc and crsdef attributes to crosssections
            gdf_cs = set_xyz_crosssections(branches, gdf_cs)

        elif crosssections_type == "point":
            # add setup point crosssections here
            raise NotImplementedError(
                f"Method {crosssections_type} is not implemented."
            )
        else:
            raise NotImplementedError(
                f"Method {crosssections_type} is not implemented."
            )

        # add crosssections to exisiting ones and update staticgeoms
        self.logger.debug(f"Adding crosssections vector to staticgeoms.")
        self.set_crosssections(gdf_cs)
        # TODO: sort out the crosssections, e.g. remove branch crosssections if point/xyz exist etc
        # TODO: setup river crosssections, set contrains based on branch types

    # def setup_branches(
    #     self,
    #     branches_fn: str,
    #     branches_ini_fn: str = None,
    #     snap_offset: float = 0.0,
    #     id_col: str = "branchId",
    #     branch_query: str = None,
    #     pipe_query: str = 'branchType == "Channel"',  # TODO update to just TRUE or FALSE keywords instead of full query
    #     channel_query: str = 'branchType == "Pipe"',
    #     **kwargs,
    # ):
    #     """This component prepares the 1D branches

    #     Adds model layers:

    #     * **branches** geom: 1D branches vector

    #     Parameters
    #     ----------
    #     branches_fn : str
    #         Name of data source for branches parameters, see data/data_sources.yml.

    #         * Required variables: branchId, branchType, # TODO: now still requires some cross section stuff

    #         * Optional variables: []

    #     """
    #     self.logger.info(f"Preparing 1D branches.")

    #     # initialise data model
    #     branches_ini = helper.parse_ini(
    #         Path(self._DATADIR).joinpath("dflowfm", f"branch_settings.ini")
    #     )  # TODO: make this default file complete, need 2 more argument, spacing? yes/no, has_in_branch crosssection? yes/no, or maybe move branches, cross sections and roughness into basemap
    #     branches = None
    #     branch_nodes = None
    #     # TODO: initilise hydrolib-core object --> build
    #     # TODO: call hydrolib-core object --> update

    #     # read branch_ini
    #     if branches_ini_fn is None or not branches_ini_fn.is_file():
    #         self.logger.warning(
    #             f"branches_ini_fn ({branches_ini_fn}) does not exist. Fall back choice to defaults. "
    #         )
    #         branches_ini_fn = Path(self._DATADIR).joinpath(
    #             "dflowfm", f"branch_settings.ini"
    #         )

    #     branches_ini.update(helper.parse_ini(branches_ini_fn))
    #     self.logger.info(f"branch default settings read from {branches_ini_fn}.")

    #     # read branches
    #     if branches_fn is None:
    #         raise ValueError("branches_fn must be specified.")

    #     branches = self._get_geodataframe(branches_fn, id_col=id_col, **kwargs)
    #     self.logger.info(f"branches read from {branches_fn} in Data Catalogue.")

    #     branches = helper.append_data_columns_based_on_ini_query(branches, branches_ini)

    #     # select branches to use
    #     if helper.check_geodataframe(branches) and branch_query is not None:
    #         branches = branches.query(branch_query)
    #         self.logger.info(f"Query branches for {branch_query}")

    #     # process branches and generate branch_nodes
    #     if helper.check_geodataframe(branches):
    #         self.logger.info(f"Processing branches")
    #         branches, branch_nodes = process_branches(
    #             branches,
    #             branch_nodes,
    #             branches_ini=branches_ini,  # TODO:  make the branch_setting.ini [global] functions visible in the setup functions. Use kwargs to allow user interaction. Make decisions on what is neccessary and what not
    #             id_col=id_col,
    #             snap_offset=snap_offset,
    #             logger=self.logger,
    #         )

    #     # validate branches
    #     # TODO: integrate below into validate module
    #     if helper.check_geodataframe(branches):
    #         self.logger.info(f"Validating branches")
    #         validate_branches(branches)

    #     # finalise branches
    #     # setup channels
    #     branches.loc[branches.query(channel_query).index, "branchType"] = "Channel"
    #     # setup pipes
    #     branches.loc[branches.query(pipe_query).index, "branchType"] = "Pipe"
    #     # assign crs
    #     branches.crs = self.crs

    #     # setup staticgeoms
    #     self.logger.debug(f"Adding branches and branch_nodes vector to staticgeoms.")
    #     self.set_staticgeoms(branches, "branches")
    #     self.set_staticgeoms(branch_nodes, "branch_nodes")

    #     # TODO: assign hydrolib-core object

    #     return branches, branch_nodes
    #
    # def setup_roughness(
    #     self,
    #     generate_roughness_from_branches: bool = True,
    #     roughness_ini_fn: str = None,
    #     branch_query: str = None,
    #     **kwargs,
    # ):
    #     """"""
    #
    #     self.logger.info(f"Preparing 1D roughness.")
    #
    #     # initialise ini settings and data
    #     roughness_ini = helper.parse_ini(
    #         Path(self._DATADIR).joinpath("dflowfm", f"roughness_settings.ini")
    #     )
    #     # TODO: how to make sure if defaults are given, we can also know it based on friction name or cross section definiation id (can we use an additional column for that? )
    #     roughness = None
    #
    #     # TODO: initilise hydrolib-core object --> build
    #     # TODO: call hydrolib-core object --> update
    #
    #     # update ini settings
    #     if roughness_ini_fn is None or not roughness_ini_fn.is_file():
    #         self.logger.warning(
    #             f"roughness_ini_fn ({roughness_ini_fn}) does not exist. Fall back choice to defaults. "
    #         )
    #         roughness_ini_fn = Path(self._DATADIR).joinpath(
    #             "dflowfm", f"roughness_settings.ini"
    #         )
    #
    #     roughness_ini.update(helper.parse_ini(roughness_ini_fn))
    #     self.logger.info(f"roughness default settings read from {roughness_ini}.")
    #
    #     if generate_roughness_from_branches == True:
    #
    #         self.logger.info(f"Generating roughness from branches. ")
    #
    #         # update data by reading user input
    #         _branches = self.staticgeoms["branches"]
    #
    #         # update data by combining ini settings
    #         self.logger.debug(
    #             f'1D roughness initialised with the following attributes: {list(roughness_ini.get("default", {}))}.'
    #         )
    #         branches = helper.append_data_columns_based_on_ini_query(
    #             _branches, roughness_ini
    #         )
    #
    #         # select branches to use e.g. to facilitate setup a selection each time setup_* is called
    #         if branch_query is not None:
    #             branches = branches.query(branch_query)
    #             self.logger.info(f"Query branches for {branch_query}")
    #
    #         # process data
    #         if helper.check_geodataframe(branches):
    #             roughness, branches = generate_roughness(branches, roughness_ini)
    #
    #         # add staticgeoms
    #         if helper.check_geodataframe(roughness):
    #             self.logger.debug(f"Updating branches vector to staticgeoms.")
    #             self.set_staticgeoms(branches, "branches")
    #
    #             self.logger.debug(f"Updating roughness vector to staticgeoms.")
    #             self.set_staticgeoms(roughness, "roughness")
    #
    #         # TODO: add hydrolib-core object
    #
    #     else:
    #
    #         # TODO: setup roughness from other data types
    #
    #         pass
    #
    # def setup_crosssections(
    #     self,
    #     generate_crosssections_from_branches: bool = True,
    #     crosssections_ini_fn: str = None,
    #     branch_query: str = None,
    #     **kwargs,
    # ):
    #     """"""
    #
    #     self.logger.info(f"Preparing 1D crosssections.")
    #
    #     # initialise ini settings and data
    #     crosssections_ini = helper.parse_ini(
    #         Path(self._DATADIR).joinpath("dflowfm", f"crosssection_settings.ini")
    #     )
    #     crsdefs = None
    #     crslocs = None
    #     # TODO: initilise hydrolib-core object --> build
    #     # TODO: call hydrolib-core object --> update
    #
    #     # update ini settings
    #     if crosssections_ini_fn is None or not crosssections_ini_fn.is_file():
    #         self.logger.warning(
    #             f"crosssection_ini_fn ({crosssections_ini_fn}) does not exist. Fall back choice to defaults. "
    #         )
    #         crosssections_ini_fn = Path(self._DATADIR).joinpath(
    #             "dflowfm", f"crosssection_settings.ini"
    #         )
    #
    #     crosssections_ini.update(helper.parse_ini(crosssections_ini_fn))
    #     self.logger.info(
    #         f"crosssections default settings read from {crosssections_ini}."
    #     )
    #
    #     if generate_crosssections_from_branches == True:
    #
    #         # set crosssections from branches (1D)
    #         self.logger.info(f"Generating 1D crosssections from 1D branches.")
    #
    #         # update data by reading user input
    #         _branches = self.staticgeoms["branches"]
    #
    #         # update data by combining ini settings
    #         self.logger.debug(
    #             f'1D crosssections initialised with the following attributes: {list(crosssections_ini.get("default", {}))}.'
    #         )
    #         branches = helper.append_data_columns_based_on_ini_query(
    #             _branches, crosssections_ini
    #         )
    #
    #         # select branches to use e.g. to facilitate setup a selection each time setup_* is called
    #         if branch_query is not None:
    #             branches = branches.query(branch_query)
    #             self.logger.info(f"Query branches for {branch_query}")
    #
    #         if helper.check_geodataframe(branches):
    #             crsdefs, crslocs, branches = generate_crosssections(
    #                 branches, crosssections_ini
    #             )
    #
    #         # update new branches with crsdef info to staticgeoms
    #         self.logger.debug(f"Updating branches vector to staticgeoms.")
    #         self.set_staticgeoms(branches, "branches")
    #
    #         # add new crsdefs to staticgeoms
    #         self.logger.debug(f"Adding crsdefs vector to staticgeoms.")
    #         self.set_staticgeoms(
    #             gpd.GeoDataFrame(
    #                 crsdefs,
    #                 geometry=gpd.points_from_xy([0] * len(crsdefs), [0] * len(crsdefs)),
    #             ),
    #             "crsdefs",
    #         )  # FIXME: make crsdefs a vector to be add to static geoms. using dummy locations --> might cause issue for structures
    #
    #         # add new crslocs to staticgeoms
    #         self.logger.debug(f"Adding crslocs vector to staticgeoms.")
    #         self.set_staticgeoms(crslocs, "crslocs")
    #
    #     else:
    #
    #         # TODO: setup roughness from other data types, e.g. points, xyz
    #
    #         pass
    #         # raise NotImplementedError()
    #
    # def setup_manholes(
    #     self,
    #     manholes_ini_fn: str = None,
    #     manholes_fn: str = None,
    #     id_col: str = None,
    #     snap_offset: float = 1,
    #     rename_map: dict = None,
    #     required_columns: list = None,
    #     required_dtypes: list = None,
    #     logger=logging,
    # ):
    #     """"""
    #
    #     self.logger.info(f"Preparing manholes.")
    #     _branches = self.staticgeoms["branches"]
    #
    #     # Setup of branches and manholes
    #     manholes, branches = delft3dfmpy_setupfuncs.setup_manholes(
    #         _branches,
    #         manholes_fn=delft3dfmpy_setupfuncs.parse_arg(
    #             manholes_fn
    #         ),  # FIXME: hydromt config parser could not parse '' to None
    #         manholes_ini_fn=delft3dfmpy_setupfuncs.parse_arg(
    #             manholes_ini_fn
    #         ),  # FIXME: hydromt config parser could not parse '' to None
    #         snap_offset=snap_offset,
    #         id_col=id_col,
    #         rename_map=delft3dfmpy_setupfuncs.parse_arg(
    #             rename_map
    #         ),  # TODO: replace with data adaptor
    #         required_columns=delft3dfmpy_setupfuncs.parse_arg(
    #             required_columns
    #         ),  # TODO: replace with data adaptor
    #         required_dtypes=delft3dfmpy_setupfuncs.parse_arg(
    #             required_dtypes
    #         ),  # TODO: replace with data adaptor
    #         logger=logger,
    #     )
    #
    #     self.logger.debug(f"Adding manholes vector to staticgeoms.")
    #     self.set_staticgeoms(manholes, "manholes")
    #
    #     self.logger.debug(f"Updating branches vector to staticgeoms.")
    #     self.set_staticgeoms(branches, "branches")
    #
    # def setup_bridges(
    #     self,
    #     roughness_ini_fn: str = None,
    #     bridges_ini_fn: str = None,
    #     bridges_fn: str = None,
    #     id_col: str = None,
    #     branch_query: str = None,
    #     snap_method: str = "overall",
    #     snap_offset: float = 1,
    #     rename_map: dict = None,
    #     required_columns: list = None,
    #     required_dtypes: list = None,
    #     logger=logging,
    # ):
    #     """"""
    #
    #     self.logger.info(f"Preparing bridges.")
    #     _branches = self.staticgeoms["branches"]
    #     _crsdefs = self.staticgeoms["crsdefs"]
    #     _crslocs = self.staticgeoms["crslocs"]
    #
    #     bridges, crsdefs = delft3dfmpy_setupfuncs.setup_bridges(
    #         _branches,
    #         _crsdefs,
    #         _crslocs,
    #         delft3dfmpy_setupfuncs.parse_arg(roughness_ini_fn),
    #         delft3dfmpy_setupfuncs.parse_arg(bridges_ini_fn),
    #         delft3dfmpy_setupfuncs.parse_arg(bridges_fn),
    #         id_col,
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             branch_query
    #         ),  # TODO: replace with data adaptor
    #         snap_method,
    #         snap_offset,
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             rename_map
    #         ),  # TODO: replace with data adaptor
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             required_columns
    #         ),  # TODO: replace with data adaptor
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             required_dtypes
    #         ),  # TODO: replace with data adaptor
    #         logger,
    #     )
    #
    #     self.logger.debug(f"Adding bridges vector to staticgeoms.")
    #     self.set_staticgeoms(bridges, "bridges")
    #
    #     self.logger.debug(f"Updating crsdefs vector to staticgeoms.")
    #     self.set_staticgeoms(crsdefs, "crsdefs")
    #
    # def setup_gates(
    #     self,
    #     roughness_ini_fn: str = None,
    #     gates_ini_fn: str = None,
    #     gates_fn: str = None,
    #     id_col: str = None,
    #     branch_query: str = None,
    #     snap_method: str = "overall",
    #     snap_offset: float = 1,
    #     rename_map: dict = None,
    #     required_columns: list = None,
    #     required_dtypes: list = None,
    #     logger=logging,
    # ):
    #     """"""
    #
    #     self.logger.info(f"Preparing gates.")
    #     _branches = self.staticgeoms["branches"]
    #     _crsdefs = self.staticgeoms["crsdefs"]
    #     _crslocs = self.staticgeoms["crslocs"]
    #
    #     gates = delft3dfmpy_setupfuncs.setup_gates(
    #         _branches,
    #         _crsdefs,
    #         _crslocs,
    #         delft3dfmpy_setupfuncs.parse_arg(roughness_ini_fn),
    #         delft3dfmpy_setupfuncs.parse_arg(gates_ini_fn),
    #         delft3dfmpy_setupfuncs.parse_arg(gates_fn),
    #         id_col,
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             branch_query
    #         ),  # TODO: replace with data adaptor
    #         snap_method,
    #         snap_offset,
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             rename_map
    #         ),  # TODO: replace with data adaptor
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             required_columns
    #         ),  # TODO: replace with data adaptor
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             required_dtypes
    #         ),  # TODO: replace with data adaptor
    #         logger,
    #     )
    #
    #     self.logger.debug(f"Adding gates vector to staticgeoms.")
    #     self.set_staticgeoms(gates, "gates")
    #
    # def setup_pumps(
    #     self,
    #     roughness_ini_fn: str = None,
    #     pumps_ini_fn: str = None,
    #     pumps_fn: str = None,
    #     id_col: str = None,
    #     branch_query: str = None,
    #     snap_method: str = "overall",
    #     snap_offset: float = 1,
    #     rename_map: dict = None,
    #     required_columns: list = None,
    #     required_dtypes: list = None,
    #     logger=logging,
    # ):
    #     """"""
    #
    #     self.logger.info(f"Preparing gates.")
    #     _branches = self.staticgeoms["branches"]
    #     _crsdefs = self.staticgeoms["crsdefs"]
    #     _crslocs = self.staticgeoms["crslocs"]
    #
    #     pumps = delft3dfmpy_setupfuncs.setup_pumps(
    #         _branches,
    #         _crsdefs,
    #         _crslocs,
    #         delft3dfmpy_setupfuncs.parse_arg(roughness_ini_fn),
    #         delft3dfmpy_setupfuncs.parse_arg(pumps_ini_fn),
    #         delft3dfmpy_setupfuncs.parse_arg(pumps_fn),
    #         id_col,
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             branch_query
    #         ),  # TODO: replace with data adaptor
    #         snap_method,
    #         snap_offset,
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             rename_map
    #         ),  # TODO: replace with data adaptor
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             required_columns
    #         ),  # TODO: replace with data adaptor
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             required_dtypes
    #         ),  # TODO: replace with data adaptor
    #         logger,
    #     )
    #
    #     self.logger.debug(f"Adding pumps vector to staticgeoms.")
    #     self.set_staticgeoms(pumps, "pumps")
    #
    # def setup_culverts(
    #     self,
    #     roughness_ini_fn: str = None,
    #     culverts_ini_fn: str = None,
    #     culverts_fn: str = None,
    #     id_col: str = None,
    #     branch_query: str = None,
    #     snap_method: str = "overall",
    #     snap_offset: float = 1,
    #     rename_map: dict = None,
    #     required_columns: list = None,
    #     required_dtypes: list = None,
    #     logger=logging,
    # ):
    #     """"""
    #
    #     self.logger.info(f"Preparing culverts.")
    #     _branches = self.staticgeoms["branches"]
    #     _crsdefs = self.staticgeoms["crsdefs"]
    #     _crslocs = self.staticgeoms["crslocs"]
    #
    #     culverts, crsdefs = delft3dfmpy_setupfuncs.setup_culverts(
    #         _branches,
    #         _crsdefs,
    #         _crslocs,
    #         delft3dfmpy_setupfuncs.parse_arg(roughness_ini_fn),
    #         delft3dfmpy_setupfuncs.parse_arg(culverts_ini_fn),
    #         delft3dfmpy_setupfuncs.parse_arg(culverts_fn),
    #         id_col,
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             branch_query
    #         ),  # TODO: replace with data adaptor
    #         snap_method,
    #         snap_offset,
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             rename_map
    #         ),  # TODO: replace with data adaptor
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             required_columns
    #         ),  # TODO: replace with data adaptor
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             required_dtypes
    #         ),  # TODO: replace with data adaptor
    #         logger,
    #     )
    #
    #     self.logger.debug(f"Adding culverts vector to staticgeoms.")
    #     self.set_staticgeoms(culverts, "culverts")
    #
    #     self.logger.debug(f"Updating crsdefs vector to staticgeoms.")
    #     self.set_staticgeoms(crsdefs, "crsdefs")
    #
    # def setup_compounds(
    #     self,
    #     roughness_ini_fn: str = None,
    #     compounds_ini_fn: str = None,
    #     compounds_fn: str = None,
    #     id_col: str = None,
    #     branch_query: str = None,
    #     snap_method: str = "overall",
    #     snap_offset: float = 1,
    #     rename_map: dict = None,
    #     required_columns: list = None,
    #     required_dtypes: list = None,
    #     logger=logging,
    # ):
    #     """"""
    #
    #     self.logger.info(f"Preparing compounds.")
    #     _structures = [
    #         self.staticgeoms[s]
    #         for s in ["bridges", "gates", "pumps", "culverts"]
    #         if s in self.staticgeoms.keys()
    #     ]
    #
    #     compounds = delft3dfmpy_setupfuncs.setup_compounds(
    #         _structures,
    #         delft3dfmpy_setupfuncs.parse_arg(roughness_ini_fn),
    #         delft3dfmpy_setupfuncs.parse_arg(compounds_ini_fn),
    #         delft3dfmpy_setupfuncs.parse_arg(compounds_fn),
    #         id_col,
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             branch_query
    #         ),  # TODO: replace with data adaptor
    #         snap_method,
    #         snap_offset,
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             rename_map
    #         ),  # TODO: replace with data adaptor
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             required_columns
    #         ),  # TODO: replace with data adaptor
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             required_dtypes
    #         ),  # TODO: replace with data adaptor
    #         logger,
    #     )
    #
    #     self.logger.debug(f"Adding compounds vector to staticgeoms.")
    #     self.set_staticgeoms(compounds, "compounds")
    #
    # def setup_boundaries(
    #     self,
    #     boundaries_fn: str = None,
    #     boundaries_fn_ini: str = None,
    #     id_col: str = None,
    #     rename_map: dict = None,
    #     required_columns: list = None,
    #     required_dtypes: list = None,
    #     logger=logging,
    # ):
    #     """"""
    #
    #     self.logger.info(f"Preparing boundaries.")
    #     _structures = [
    #         self.staticgeoms[s]
    #         for s in ["bridges", "gates", "pumps", "culverts"]
    #         if s in self.staticgeoms.keys()
    #     ]
    #
    #     boundaries = delft3dfmpy_setupfuncs.setup_boundaries(
    #         delft3dfmpy_setupfuncs.parse_arg(boundaries_fn),
    #         delft3dfmpy_setupfuncs.parse_arg(boundaries_fn_ini),
    #         id_col,
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             rename_map
    #         ),  # TODO: replace with data adaptor
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             required_columns
    #         ),  # TODO: replace with data adaptor
    #         delft3dfmpy_setupfuncs.parse_arg(
    #             required_dtypes
    #         ),  # TODO: replace with data adaptor
    #         logger,
    #     )
    #
    #     self.logger.debug(f"Adding boundaries vector to staticgeoms.")
    #     self.set_staticgeoms(boundaries, "boundaries")
    #
    # def _setup_datamodel(self):
    #
    #     """setup data model using dfm and drr naming conventions"""
    #     if self._datamodel == None:
    #         self._datamodel = delft3dfmpy_setupfuncs.setup_dm(
    #             self.staticgeoms, logger=self.logger
    #         )
    #
    # def setup_dflowfm(
    #     self,
    #     model_type: str = "1d",
    #     one_d_mesh_distance: float = 40,
    # ):
    #     """ """
    #     self.logger.info(f"Preparing DFlowFM 1D model.")
    #
    #     self._setup_datamodel()
    #
    #     self._dfmmodel = delft3dfmpy_setupfuncs.setup_dflowfm(
    #         self._datamodel,
    #         model_type=model_type,
    #         one_d_mesh_distance=one_d_mesh_distance,
    #         logger=self.logger,
    #     )
    #
    # ## I/O

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
            self.write_config()
        if self._staticmaps:
            self.write_staticmaps()
        if self._staticgeoms:
            self.write_staticgeoms()
        if self.dfmmodel:
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
            fn_out = join(self.root, "staticgeoms", f"{name}.geojson")
            # FIXME: does not work if does not reset index
            gdf.reset_index(drop=True).to_file(fn_out, driver="GeoJSON")

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

        # write friction
        self._write_friction()  # FIXME: ask Rinske, add global section correctly

        # write crosssections
        self._write_crosssections()  # FIXME None handling, if there are no crosssections

        # save model
        self.dfmmodel.save(recurse=True)

    def _write_friction(self):

        #
        frictions = self._staticgeoms["branches"][
            ["frictionId", "frictionValue", "frictionType"]
        ]
        frictions = frictions.drop_duplicates(subset="frictionId")

        self.dfmmodel.geometry.frictfile = []
        # create a new friction
        for i, row in frictions.iterrows():
            fric_model = FrictionModel(global_=row.to_dict())
            fric_model.filepath = f"roughness_{i}.ini"
            self.dfmmodel.geometry.frictfile.append(fric_model)

    def _write_crosssections(self):
        """write crosssections into hydrolib-core crsloc and crsdef objects"""

        # preprocessing for crosssections from staticgeoms
        gpd_crs = self._staticgeoms["crosssections"]

        # crsdef
        # get crsdef from crosssections gpd # FIXME: change this for update case
        gpd_crsdef = gpd_crs[[c for c in gpd_crs.columns if c.startswith("crsdef")]]
        gpd_crsdef = gpd_crsdef.rename(
            columns={c: c.split("_")[1] for c in gpd_crsdef.columns}
        )
        # TODO: check if CrossDefModel drops duplicates definitionId else drop here first
        crsdef = CrossDefModel(definition=gpd_crsdef.to_dict("records"))
        self.dfmmodel.geometry.crossdeffile = crsdef

        # crsloc
        # get crsloc from crosssections gpd # FIXME: change this for update case
        gpd_crsloc = gpd_crs[[c for c in gpd_crs.columns if c.startswith("crsloc")]]
        gpd_crsloc = gpd_crsloc.rename(
            columns={c: c.split("_")[1] for c in gpd_crsloc.columns}
        )

        crsloc = CrossLocModel(crosssection=gpd_crsloc.to_dict("records"))
        self.dfmmodel.geometry.crosslocfile = crsloc

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
        # return pyproj.CRS.from_epsg(self.get_config("global.epsg", fallback=4326))
        return self.region.crs

    @property
    def dfmmodel(self):
        if self._dfmmodel == None:
            self.init_dfmmodel()
        return self._dfmmodel

    def init_dfmmodel(self):
        # Create output directories
        outputdir = Path(self.root).joinpath("dflowfm")
        outputdir.mkdir(parents=True, exist_ok=True)
        # TODO: check that HydroMT already wrote the updated config
        # create a new MDU-Model
        self._dfmmodel = FMModel(filepath=Path(join(self.root, self._config_fn)))
        self._dfmmodel.geometry.netfile = NetworkModel()
        self._dfmmodel.geometry.netfile.filepath = (
            "fm_net.nc"  # because hydrolib.core writes this argument as absolute path
        )
        self._dfmmodel.geometry.crossdeffile = CrossDefModel()
        self._dfmmodel.geometry.crossdeffile.filepath = outputdir.joinpath("crsdef.ini")
        self._dfmmodel.geometry.crosslocfile = CrossLocModel()
        self._dfmmodel.geometry.crosslocfile.filepath = outputdir.joinpath("crsloc.ini")
        # self._dfmmodel.geometry.frictfile = [FrictionModel()]
        # self._dfmmodel.geometry.frictfile[0].filepath = outputdir.joinpath(
        #    "roughness.ini"
        # )

    @property
    def branches(self):
        """
        Returns the branches (gpd.GeoDataFrame object) representing the 1D network.
        Contains several "branchType" for : channel, river, pipe, tunnel.
        """
        if self._branches is None:
            # self.read_branches() #not implemented yet
            self._branches = gpd.GeoDataFrame(crs=self.crs)
        return self._branches

    def set_branches(self, branches: gpd.GeoDataFrame):
        """Updates the branches object as well as the linked staticgeoms."""
        # Check if "branchType" col in new branches
        if "branchType" in branches.columns:
            self._branches = branches
        else:
            self.logger.error(
                "'branchType' column absent from the new branches, could not update."
            )
        # Update channels/pipes in staticgeoms
        _ = self.set_branches_component(name="river")
        _ = self.set_branches_component(name="channel")
        _ = self.set_branches_component(name="pipe")

        # update staticgeom #FIXME: do we need branches as staticgeom?
        self.logger.debug(f"Adding branches vector to staticgeoms.")
        self.set_staticgeoms(gpd.GeoDataFrame(branches, crs=self.crs), "branches")

        self.logger.debug(f"Updating branches in network.")

    def add_branches(
        self,
        new_branches: gpd.GeoDataFrame,
        branchtype: str,
        node_distance: float = 40.0,
    ):
        """Add new branches of branchtype to the branches object"""
        branches = self.branches.copy()
        # Check if "branchType" in new_branches column, else add
        if "branchType" not in new_branches.columns:
            new_branches["branchType"] = np.repeat(branchtype, len(new_branches.index))
        branches = branches.append(new_branches, ignore_index=True)
        # Check if we need to do more check/process to make sure everything is well connected
        validate_branches(branches)
        # # Add to dfmmodel network
        mesh.mesh1d_add_branch(
            self.dfmmodel.geometry.netfile.network,
            new_branches.geometry.to_list(),
            node_distance=node_distance,
            branch_names=new_branches.branchId.to_list(),
            branch_orders=new_branches.branchOrder.to_list(),
        )
        self.set_branches(branches)

    def set_branches_component(self, name):
        gdf_comp = self.branches[self.branches["branchType"] == name]
        if gdf_comp.index.size > 0:
            self.set_staticgeoms(gdf_comp, name=f"{name}s")
        return gdf_comp

    @property
    def rivers(self):
        if "rivers" in self.staticgeoms:
            gdf = self.staticgeoms["rivers"]
        else:
            gdf = self.set_branches_component("rivers")
        return gdf

    @property
    def channels(self):
        if "channels" in self.staticgeoms:
            gdf = self.staticgeoms["channels"]
        else:
            gdf = self.set_branches_component("channel")
        return gdf

    @property
    def pipes(self):
        if "pipes" in self.staticgeoms:
            gdf = self.staticgeoms["pipes"]
        else:
            gdf = self.set_branches_component("pipe")
        return gdf

    @property
    def opensystem(self):
        gdf = self.branches[self.branches["branchType"].isin(["river", "channel"])]
        return gdf

    @property
    def closedsystem(self):
        gdf = self.branches[self.branches["branchType"].isin(["pipe", "tunnel"])]
        return gdf

    @property
    def crosssections(self):
        """Quick accessor to crosssections staticgeoms"""
        if "crosssections" in self.staticgeoms:
            gdf = self.staticgeoms["crosssections"]
        else:
            gdf = gpd.GeoDataFrame(crs=self.crs)
        return gdf

    def set_crosssections(self, crosssections: gpd.GeoDataFrame):
        """Updates crosssections in staticgeoms with new ones"""
        if len(self.crosssections) > 0:
            crosssections = gpd.GeoDataFrame(
                pd.concat([self.crosssections, crosssections]), crs=self.crs
            )
        self.set_staticgeoms(crosssections, name="crosssections")
