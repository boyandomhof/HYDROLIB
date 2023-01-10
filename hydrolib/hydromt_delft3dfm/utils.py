from pathlib import Path
from os.path import join
from typing import Dict, Union, Tuple, List
from enum import Enum

import geopandas as gpd
import pandas as pd
import xarray as xr
import numpy as np
from shapely.geometry import Point, LineString

from hydrolib.core.dflowfm import (
    FMModel,
    FrictionModel,
    CrossDefModel,
    CrossLocModel,
    StorageNodeModel,
    ExtModel,
    Boundary,
    ForcingModel,
    BranchModel,
)

from .workflows import helper

__all__ = [
    "read_branches_gui",
    "write_branches_gui",
    "read_crosssections",
    "write_crosssections",
    "read_friction",
    "write_friction",
    "read_manholes",
    "write_manholes",
    "read_1dboundary",
    "write_1dboundary",
]


def read_branches_gui(
    gdf: gpd.GeoDataFrame,
    fm_model: FMModel,
) -> gpd.GeoDataFrame:
    """
    Read branches.gui and add the properties to branches geodataframe

    Parameters
    ----------
    gdf: geopandas.GeoDataFrame
        gdf containing the branches
    fm_model: hydrolib.core FMModel
        DflowFM model object from hydrolib

    Returns
    -------
    gdf_out: geopandas.GeoDataFrame
        gdf containing the branches updated with branches gui params
    """
    branchgui_model = BranchModel()
    filepath = fm_model.filepath.with_name(
        branchgui_model._filename() + branchgui_model._ext()
    )

    if not filepath.is_file():
        # Create df with all attributes from nothing; all branches are considered as river
        df_gui = pd.DataFrame()
        df_gui["branchid"] = [b.branchid for b in gdf.itertuples()]
        df_gui["branchtype"] = "river"
        df_gui["manhole_up"] = ""
        df_gui["manhole_dn"] = ""

    else:
        # Create df with all attributes from gui
        branchgui_model = BranchModel(filepath)
        br_list = branchgui_model.branch
        df_gui = pd.DataFrame()
        df_gui["branchid"] = [b.name for b in br_list]
        df_gui["branchtype"] = [b.branchtype for b in br_list]
        df_gui["manhole_up"] = [b.sourcecompartmentname for b in br_list]
        df_gui["manhole_dn"] = [b.targetcompartmentname for b in br_list]

    # Adapt type and add close attribute
    # TODO: channel and tunnel types are not defined
    df_gui["branchtype"] = df_gui["branchtype"].replace(
        {
            0: "river",
            2: "pipe",
            1: "sewerconnection",
        }
    )

    # Merge the two df based on branchid
    df_gui = df_gui.drop_duplicates(subset="branchid")
    gdf_out = gdf.merge(df_gui, on="branchid", how="left")

    return gdf_out


def write_branches_gui(
    gdf: gpd.GeoDataFrame,
    savedir: str,
) -> str:
    """
    write branches.gui file from branches geodataframe

    Parameters
    ----------
    gdf: geopandas.GeoDataFrame
        gdf containing the branches
    savedir: str
        path to the directory where to save the file.
    manholes: geopandas.GeoDataFrame, optional
        If exists add manholes attributes

    Returns
    -------
    branchgui_fn: str
        relative filepath to branches_gui file.

    #TODO: branches.gui is written with a [general] section which is not recongnised by GUI. Improvement of the GUI is needed.
    #TODO: branches.gui has a column is custumised length written as bool, which is not recongnised by GUI. improvement of the hydrolib-core writer is needed.
    """

    if not gdf["branchtype"].isin(["pipe", "tunnel"]).any():
        gdf[["manhole_up", "manhole_dn"]] = ""

    branches = gdf[["branchid", "branchtype", "manhole_up", "manhole_dn"]]
    branches = branches.rename(
        columns={
            "branchid": "name",
            "manhole_up": "sourceCompartmentName",
            "manhole_dn": "targetCompartmentName",
        }
    )
    branches["branchtype"] = branches["branchtype"].replace(
        {"river": 0, "pipe": 2, "sewerconnection": 1}
    )
    branchgui_model = BranchModel(branch=branches.to_dict("records"))
    branchgui_fn = branchgui_model._filename() + branchgui_model._ext()
    branchgui_model.filepath = join(savedir, branchgui_fn)
    branchgui_model.save()

    return branchgui_fn


def read_crosssections(
    gdf: gpd.GeoDataFrame, fm_model: FMModel
) -> tuple((gpd.GeoDataFrame, gpd.GeoDataFrame)):
    """
    Read crosssections from hydrolib-core crsloc and crsdef objects and add to branches.
    Also returns crosssections geodataframe.

    Parameters
    ----------
    gdf: geopandas.GeoDataFrame
        gdf containing the branches
    fm_model: hydrolib.core FMModel
        DflowFM model object from hydrolib

    Returns
    -------
    gdf_crs: geopandas.GeoDataFrame
        geodataframe copy of the cross-sections and data
    """

    def _list2Str(lst):

        if type(lst) is list:
            # apply conversion to list columns
            if isinstance(lst[0], float):
                return " ".join(["{}".format(i) for i in lst])
            elif isinstance(lst[0], str):
                return " ".join(["{}".format(i) for i in lst])
        else:
            return lst

    # Start with crsdef to create the crosssections attributes
    crsdef = fm_model.geometry.crossdeffile
    crs_dict = dict()
    for b in crsdef.definition:
        crs_dict[b.id] = b.__dict__
    df_crsdef = pd.DataFrame.from_dict(crs_dict, orient="index")
    df_crsdef = df_crsdef.drop("comments", axis=1)
    df_crsdef["crs_id"] = df_crsdef["id"]  # column to merge
    # convertion needed  for xyz/zw crossections
    # convert list to str ()
    df_crsdef = df_crsdef.applymap(lambda x: _list2Str(x))
    # convert float to int
    int_columns = set(df_crsdef.columns).intersection(("xyzcount", "sectioncount"))
    df_crsdef.loc[:, int_columns] = (
        df_crsdef.loc[:, int_columns]
        .fillna(-999)
        .astype(int)
        .astype(object)
        .where(df_crsdef.loc[:, int_columns].notnull())
    )
    # Rename to prepare for crossection geom
    _gdf_crsdef = df_crsdef.rename(
        columns={c: f"crsdef_{c}" for c in df_crsdef.columns if c != "crs_id"}
    )
    _gdf_crsdef = _gdf_crsdef.rename(columns={"crsdef_frictionid": "crsdef_frictionid"})

    # Continue with locs to get the locations and branches id
    crsloc = fm_model.geometry.crosslocfile
    crsloc_dict = dict()
    for b in crsloc.crosssection:
        crsloc_dict[b.id] = b.__dict__
    df_crsloc = pd.DataFrame.from_dict(crsloc_dict, orient="index")
    df_crsloc = df_crsloc.drop("comments", axis=1)
    df_crsloc["crs_id"] = df_crsloc["definitionid"]  # column to merge
    # convert locationtype from enum to str (due to hydrolib-core bug)
    if isinstance(df_crsloc["locationtype"][0], Enum):
        df_crsloc["locationtype"] = df_crsloc["locationtype"].apply(lambda x: x.value)
    # get crsloc geometry
    if df_crsloc.dropna(axis=1).columns.isin(["x", "y"]).all():
        df_crsloc["geometry"] = [
            Point(i.x, i.y) for i in df_crsloc[["x", "y"]].itertuples()
        ]
    else:
        _gdf = gdf.set_index("branchid")
        df_crsloc["geometry"] = [
            _gdf.loc[i.branchid, "geometry"].interpolate(i.chainage)
            for i in df_crsloc.itertuples()
        ]
    # Rename to prepare for crossection geom
    _gdf_crsloc = df_crsloc.rename(
        columns={
            c: f"crsloc_{c}"
            for c in df_crsloc.columns
            if c not in ["crs_id", "geometry"]
        }
    )

    # Combine def attributes with locs for crossection geom
    gdf_crs = _gdf_crsloc.merge(_gdf_crsdef, on="crs_id")
    gdf_crs = gpd.GeoDataFrame(gdf_crs, crs=gdf.crs)

    return gdf_crs


def write_crosssections(gdf: gpd.GeoDataFrame, savedir: str) -> Tuple[str, str]:
    """write crosssections into hydrolib-core crsloc and crsdef objects

    Parameters
    ----------
    gdf: geopandas.GeoDataFrame
        gdf containing the crosssections
    savedir: str
        path to the directory where to save the file.

    Returns
    -------
    crsdef_fn: str
        relative filepath to crsdef file.
    crsloc_fn: str
        relative filepath to crsloc file.
    """
    # crsdef
    # get crsdef from crosssections gpd
    gpd_crsdef = gdf[[c for c in gdf.columns if c.startswith("crsdef")]]
    gpd_crsdef = gpd_crsdef.rename(
        columns={c: c.removeprefix("crsdef_") for c in gpd_crsdef.columns}
    )
    gpd_crsdef = gpd_crsdef.drop_duplicates(subset="id")
    gpd_crsdef = gpd_crsdef.astype(object).replace(np.nan, None)
    crsdef = CrossDefModel(definition=gpd_crsdef.to_dict("records"))
    # fm_model.geometry.crossdeffile = crsdef

    crsdef_fn = crsdef._filename() + ".ini"
    crsdef.save(
        join(savedir, crsdef_fn),
        recurse=False,
    )

    # crsloc
    # get crsloc from crosssections gpd
    gpd_crsloc = gdf[[c for c in gdf.columns if c.startswith("crsloc")]]
    gpd_crsloc = gpd_crsloc.rename(
        columns={c: c.removeprefix("crsloc_") for c in gpd_crsloc.columns}
    )

    # add x,y column --> hydrolib value_error: branchid and chainage or x and y should be provided
    # x,y would make reading back much faster than re-computing from branchid and chainage....
    # xs, ys = np.vectorize(lambda p: (p.xy[0][0], p.xy[1][0]))(gdf["geometry"])
    # gpd_crsloc["x"] = xs
    # gpd_crsloc["y"] = ys

    crsloc = CrossLocModel(crosssection=gpd_crsloc.to_dict("records"))

    crsloc_fn = crsloc._filename() + ".ini"
    crsloc.save(
        join(savedir, crsloc_fn),
        recurse=False,
    )

    return crsdef_fn, crsloc_fn


def read_friction(gdf: gpd.GeoDataFrame, fm_model: FMModel) -> gpd.GeoDataFrame:
    """
    read friction files and add properties to branches geodataframe.
    assumes cross-sections have been read before to contain per branch frictionid

    Parameters
    ----------
    gdf: geopandas.GeoDataFrame
        gdf containing the crosssections
    fm_model: hydrolib.core FMModel
        DflowFM model object from hydrolib

    Returns
    -------
    gdf_out: geopandas.GeoDataFrame
        gdf containing the crosssections updated with the friction params
    """
    fric_list = fm_model.geometry.frictfile
    # TODO: check if read/write crosssections can automatically parse it?

    # Create dictionnaries with all attributes from fricfile
    # For now assume global only
    fricval = dict()
    frictype = dict()
    for i in range(len(fric_list)):
        for j in range(len(fric_list[i].global_)):
            fricval[fric_list[i].global_[j].frictionid] = (
                fric_list[i].global_[j].frictionvalue
            )
            frictype[fric_list[i].global_[j].frictionid] = (
                fric_list[i].global_[j].frictiontype
            )

    # Create friction value and type by replacing frictionid values with dict
    gdf_out = gdf.copy()
    gdf_out["frictionvalue"] = gdf_out["crsdef_frictionid"]
    gdf_out["frictionvalue"] = gdf_out["frictionvalue"].replace(fricval)
    gdf_out["frictiontype"] = gdf_out["crsdef_frictionid"]
    gdf_out["frictiontype"] = gdf_out["frictiontype"].replace(frictype)

    return gdf_out


def write_friction(gdf: gpd.GeoDataFrame, savedir: str) -> List[str]:
    """
    write friction files from crosssections geodataframe

    Parameters
    ----------
    gdf: geopandas.GeoDataFrame
        gdf containing the crosssections
    savedir: str
        path to the directory where to save the file.

    Returns
    -------
    friction_fns: List of str
        list of relative filepaths to friction files.
    """
    frictions = gdf[["crsdef_frictionid", "frictionvalue", "frictiontype"]]
    # Remove nan
    frictions = frictions.rename(columns={"crsdef_frictionid": "frictionid"})
    frictions = frictions.dropna(subset="frictionid")
    # For xyz crosssections, column name is frictionids instead of frictionid
    if "crsdef_frictionids" in gdf:
        # For now assume unique and not list
        frictionsxyz = gdf[["crsdef_frictionids", "frictionvalue", "frictiontype"]]
        frictionsxyz = frictionsxyz.dropna(subset="crsdef_frictionids")
        frictionsxyz = frictionsxyz.rename(columns={"crsdef_frictionids": "frictionid"})
        frictions = pd.concat([frictions, frictionsxyz])
    frictions = frictions.drop_duplicates(subset="frictionid")

    friction_fns = []
    # create a new friction
    for i, row in frictions.iterrows():
        fric_model = FrictionModel(global_=row.to_dict())
        fric_name = f"{row.frictiontype[0]}-{str(row.frictionvalue).replace('.', 'p')}"
        fric_filename = f"{fric_model._filename()}_{fric_name}" + fric_model._ext()
        fric_model.filepath = join(savedir, fric_filename)
        fric_model.save(fric_model.filepath, recurse=False)

        # save relative path to mdu
        friction_fns.append(fric_filename)

    return friction_fns


def read_manholes(gdf: gpd.GeoDataFrame, fm_model: FMModel) -> gpd.GeoDataFrame:
    """
    Read manholes from hydrolib-core storagenodes and network 1d nodes for locations.
    Returns manholes geodataframe.

    Parameters
    ----------
    gdf: geopandas.GeoDataFrame
        gdf containing the network1d nodes
    fm_model: hydrolib.core FMModel
        DflowFM model object from hydrolib

    Returns
    -------
    gdf_manholes: geopandas.GeoDataFrame
        geodataframe of the manholes and data
    """
    manholes = fm_model.geometry.storagenodefile
    # Parse to dict and DataFrame
    manholes_dict = dict()
    for b in manholes.storagenode:
        manholes_dict[b.id] = b.__dict__
    df_manholes = pd.DataFrame.from_dict(manholes_dict, orient="index")

    # Drop variables
    df_manholes = df_manholes.drop(
        ["comments"],
        axis=1,
    )
    # Rename case sensitive
    df_manholes = df_manholes.rename(
        columns={
            "manholeid": "manholeid",
            "nodeid": "nodeid",
            "usetable": "usetable",
            "bedlevel": "bedlevel",
            "streetlevel": "streetlevel",
            "streetstoragearea": "streetstoragearea",
            "storagetype": "storagetype",
        }
    )

    gdf_manholes = gpd.GeoDataFrame(
        df_manholes.merge(gdf, on="nodeid", how="left"), crs=gdf.crs
    )

    return gdf_manholes


def write_manholes(gdf: gpd.GeoDataFrame, savedir: str) -> str:
    """
    write manholes into hydrolib-core storage nodes objects

    Parameters
    ----------
    gdf: geopandas.GeoDataFrame
        gdf containing the manholes
    savedir: str
        path to the directory where to save the file.

    Returns
    -------
    storage_fn: str
        relative path to storage nodes file.
    """
    storagenodes = StorageNodeModel(storagenode=gdf.to_dict("records"))

    storage_fn = storagenodes._filename() + ".ini"
    storagenodes.save(
        join(savedir, storage_fn),
        recurse=False,
    )

    return storage_fn


def read_1dboundary(
    df: pd.DataFrame, quantity: str, nodes: gpd.GeoDataFrame
) -> xr.DataArray:
    """
    Read for a specific quantity the corresponding external and forcing files and parse to xarray
    # TODO: support external forcing for 2D

    Parameters
    ----------
    df: pd.DataFrame
        External Model DataFrame filtered for quantity.
    quantity: str
        Name of quantity (eg 'waterlevel').
    nodes: gpd.GeoDataFrame
        Nodes locations of the boundary in df.

    Returns
    -------
    da_out: xr.DataArray
        External and focing values combined into a DataArray for variable quantity.
    """
    # Initialise dataarray attributes
    bc = {"quantity": quantity}
    nodeids = df.nodeid.values
    # Assume one forcing file (hydromt writer) and read
    forcing = df.forcingfile.iloc[0]
    df_forcing = pd.DataFrame([f.__dict__ for f in forcing.forcing])
    # Filter for the current nodes
    df_forcing = df_forcing[np.isin(df_forcing.name, nodeids)]

    # Get data
    # Check if all constant
    if np.all(df_forcing.function == "constant"):
        # Prepare data
        data = np.array([v[0][0] for v in df_forcing.datablock])
        data = data + df_forcing.offset.values * df_forcing.factor.values
        # Prepare dataarray properties
        dims = ["index"]
        coords = dict(index=nodeids)
        bc["function"] = "constant"
        bc["units"] = df_forcing.quantityunitpair.iloc[0][0].unit
        bc["factor"] = 1
        bc["offset"] = 0
    # Check if all timeseries
    elif np.all(df_forcing.function == "timeseries"):
        # Prepare data
        data = list()
        for i in np.arange(len(df_forcing.datablock)):
            v = df_forcing.datablock.iloc[i]
            offset = df_forcing.offset.iloc[i]
            factor = df_forcing.factor.iloc[i]
            databl = [n[1] * factor + offset for n in v]
            data.append(databl)
        data = np.array(data)
        # Assume unique times
        times = np.array([n[0] for n in df_forcing.datablock.iloc[0]])
        # Prepare dataarray properties
        dims = ["index", "time"]
        coords = dict(index=nodeids, time=times)
        bc["function"] = "timeseries"
        bc["units"] = df_forcing.quantityunitpair.iloc[0][1].unit
        bc["time_unit"] = df_forcing.quantityunitpair.iloc[0][0].unit
        bc["factor"] = 1
        bc["offset"] = 0
    # Else not implemented yet
    else:
        raise NotImplementedError(
            f"ForcingFile with several function for a single variable not implemented yet. Skipping reading forcing for variable {quantity}."
        )

    # Get nodeid coordinates
    node_geoms = nodes.set_index("nodeid").reindex(nodeids)
    xs, ys = np.vectorize(lambda p: (p.xy[0][0], p.xy[1][0]))(node_geoms["geometry"])
    coords["x"] = ("index", xs)
    coords["y"] = ("index", ys)

    # Prep DataArray and add to forcing
    da_out = xr.DataArray(
        data=data,
        dims=dims,
        coords=coords,
        attrs=bc,
    )
    da_out.name = f"{quantity}bnd"

    return da_out


def write_1dboundary(forcing: Dict, savedir: str) -> Tuple:
    """ "
    write 1dboundary ext and boundary files from forcing dict

    Parameters
    ----------
    forcing: dict of xarray DataArray
        Dict of boundary DataArray for each variable
    savedir: str
        path to the directory where to save the file.

    Returns
    -------
    forcing_fn: str
        relative path to boundary forcing file.
    ext_fn: str
        relative path to external boundary file.
    """
    extdict = list()
    bcdict = list()
    # Loop over forcing dict
    for name, da in forcing.items():
        for i in da.index.values:
            bc = da.attrs.copy()
            # Boundary
            ext = dict()
            ext["quantity"] = bc["quantity"]
            ext["nodeid"] = i
            extdict.append(ext)
            # Forcing
            bc["name"] = i
            if bc["function"] == "constant":
                # one quantityunitpair
                bc["quantityunitpair"] = [{"quantity": da.name, "unit": bc["units"]}]
                # only one value column (no times)
                bc["datablock"] = [[da.sel(index=i).values.item()]]
            else:
                # two quantityunitpair
                bc["quantityunitpair"] = [
                    {"quantity": "time", "unit": bc["time_unit"]},
                    {"quantity": da.name, "unit": bc["units"]},
                ]
                bc.pop("time_unit")
                # time/value datablock
                bc["datablock"] = [
                    [t, x] for t, x in zip(da.time.values, da.sel(index=i).values)
                ]
            bc.pop("quantity")
            bc.pop("units")
            bcdict.append(bc)

    forcing_model = ForcingModel(forcing=bcdict)
    forcing_fn = forcing_model._filename() + ".bc"

    ext_model = ExtModel()
    ext_fn = ext_model._filename() + ".ext"
    for i in range(len(extdict)):
        ext_model.boundary.append(
            Boundary(**{**extdict[i], "forcingFile": forcing_model})
        )
    # Save ext and forcing files
    ext_model.save(join(savedir, ext_fn), recurse=True)

    return forcing_fn, ext_fn
