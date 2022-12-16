# =============================================================================
#
# License: LGPL
#
# Author: Arjon Buijert Arcadis
#
# =============================================================================


import os
from datetime import datetime

import geopandas as gpd
import numpy as np
import pandas as pd
import xarray as xr
from read_dhydro import net_nc2gdf, read_nc_data


def read_params(input_nc):
    ds = xr.open_dataset(input_nc)
    EPSG = ds["projected_coordinate_system"].EPSG_code
    if EPSG == "EPSG:0":
        print("Geen projectie in het model, Amersfoort aangenomen")
        EPSG = "EPSG:28992"

    # User can give input to which parameter is needed.
    choice_params = [x for x in list(ds.variables) if x.startswith("mesh1d_")]
    choice_params.append([x for x in list(ds.variables) if x.startswith("Mesh2d_")])
    print("The possible parameters are:\n", choice_params)
    return choice_params


def statistics_dhydro(
    input_path, par, sdate="", edate="", output_file="/tmp/dhydro.shp", stat=""
):
    """
    Determine simple statistics of nc-file and write to shp, xlsx or csv.
    ___________________________________________________________________________________________________________

    Parameters:
        input_path : str
            Path to input nc-file
        par: str
            Needed parameter of nc-file
            for example: 'mesh1d_s1', 'mesh1d_s0', 'mesh1d_waterdepth', 'mesh1d_u1', 'mesh1d_u0', mesh1d_q1'
        sdate: str
            Start date of data
        edate: str
            End date of data
        output_file : str
            Path to result file (shp,xlsx or csv)
    ___________________________________________________________________________________________________________

    Returns:
        A shp, xlsx or csv file with some basics statistics for the chosen parameter

    """

    # stappen
    # Focus op waterstanden, waterdieptes, linkjes, waterdieptes
    # obv coordinates in dataset baseren.\

    # check extension
    extension = os.path.splitext(output_file)[-1].lower()
    if extension not in [".csv", ".xlsx", ".shp", ".tif"]:
        print(
            "Onbekende extensie "
            + str(extension)
            + "\n"
            + "Geen output file gegenereerd"
        )

    # read netcdf file
    if str(type(input_path)) == "<class 'netCDF4._netCDF4.Dataset'>":
        ds = input_path
    else:
        ds = xr.open_dataset(input_path)

    EPSG = "EPSG:" + str(ds["projected_coordinate_system"].epsg)
    variables = list(ds.variables)

    if not par in variables:
        raise ValueError("Parameter '" + str(par) + "'niet in nc-file")

    # =============================================================================
    #     # determine relevant parameters
    #     ds_params = [x for x in list(ds.variables) if x.startswith(ds[par].mesh + "_" + ds[par].location)]
    #     ds_params_coords = list(ds[par].coords)[0:2]
    # =============================================================================

    # create dataframe with data
    df = read_nc_data(ds, par)

    # TODO datumcontrole
    sdata = min(df.index)
    edata = max(df.index)

    # filter data based on time
    if sdate != "":
        if sdate > edata:
            raise ("start date later then end date in model results")
        else:
            df = df[df.index >= datetime.strptime(sdate, "%Y/%m/%d")]
    if edate != "":
        if edate < sdata:
            raise ("end date earlier then start date in model results")
        else:
            df = df[df.index <= datetime.strptime(edate, "%Y/%m/%d")]

    # create geometry
    # TODO als het een edge is werkt het nog niet.

    if "mesh2d" in ds[par].mesh.lower():
        network_type = "2d_faces"
    elif "mesh1d" in ds[par].mesh.lower():
        network_type = "1d_meshnodes" if "node" in ds[par].location else "1d_edges"
    else:
        raise Exception("Onbekende celsoort, check de code en waardes.")
    # =============================================================================
    #     network_type = (
    #         "2d_faces" if "mesh2d" in ds[par].mesh.lower() elif "1d_meshnodes"
    #     )  # todo line and structure info    gdfs = net_nc2gdf(input_path,results=[network_type])
    # =============================================================================
    gdfs = net_nc2gdf(input_path, results=[network_type])
    gdf = gdfs[
        network_type.lower() if network_type not in list(gdfs.keys()) else network_type
    ]

    # determine statistics
    if stat == "":
        df_stat = df.describe(percentiles=[0.5])
        df_stat = df_stat.transpose()
        gdf = gdf.merge(df_stat, left_on="id", right_index=True)
    elif stat == "max":
        df_stat = pd.DataFrame(df.max(), columns=["max"])
        gdf = gdf.merge(df_stat, left_on="id", right_index=True)

    # prevent lists in data
    for column in list(gdf.columns):
        if isinstance(gdf[column].iloc[0], list):
            gdf[column] = [",".join(map(str, l)) for l in gdf[column]]

    # export data
    if extension in [".csv", ".xlsx", ".shp", ".tif"]:
        if extension == ".xlsx":
            if isinstance(gdf, pd.DataFrame):
                gdf.to_excel(
                    output_file, float_format="%.3f", sheet_name=str(par), index=True
                )
            else:
                df_stat.to_excel(
                    output_file, float_format="%.3f", sheet_name=str(par), index=True
                )
        elif extension == ".shp":
            if isinstance(gdf, pd.DataFrame):
                gdf.to_file(output_file)
            else:
                print("Shape maken zonder geometrie niet mogelijk")
        elif extension == ".csv":
            if isinstance(gdf, pd.DataFrame):
                gdf.to_csv(output_file)
            else:
                df_stat.to_csv(output_file)
        elif extension == ".tif":
            import rasterio
            from shapely.geometry import MultiPoint, Polygon

            geom_area = MultiPoint(gdf.geometry).convex_hull
            gdf_area = gpd.GeoDataFrame(
                index=[0], crs="EPSG:28992", geometry=[geom_area]
            )
            meta = "fout!!" "<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<"
            with rasterio.open(output_file, "w+", **meta) as out:
                out_arr = out.read(1)
                shapes = ((geom, value) for geom, value in zip(gdf.geometry, gdf[stat]))
                level = rasterio.features.rasterize(
                    shapes=shapes, fill=0, out=out_arr, transform=out.transform
                )
                level[level <= -999] = np.nan
                out.write_band(1, level)

    return gdf


if __name__ == "__main__":
    input_path = r"C:\Users\devop\Documents\Scripts\Hydrolib\HYDROLIB\contrib\Arcadis\scripts\exampledata\Dellen\Model_cleaned\dflowfm\output\Flow1D_map.nc"
    output_file = r"C:\Users\devop\Desktop"
    read_params(input_path)
    par = "mesh1d_q1"
    test = statistics_dhydro(
        input_path, par, sdate="", edate="", output_file="", stat=""
    )
