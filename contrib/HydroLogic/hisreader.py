# -*- coding: utf-8 -*-
"""
Created on Tue Dec 14 10:53:59 2021

@author: Koen Reef Hydrologic
"""

# =============================================================================
# Import
# =============================================================================
import datetime as dt
import os
import pathlib
from datetime import datetime
from typing import List, Literal, Optional, Type, TypeVar

import matplotlib.pyplot as plt
import netCDF4 as nc
import numpy as np
import pandas as pd
import ugfile as uf
from pydantic import BaseModel, Field

from hydrolib.core.io.structure.models import Structure

PandasDataFrame = TypeVar("pandas.core.frame.DataFrame")

"""============================================================================
Provides
    1. Creates an object to convert variables from the his netcdf output files
    from the D-Hydro model (...his.nc) into python objects
    Use: HisResults(inputdir, outputdir)
        Parameters
        ----------
        inputdir : absolute windows directory path to the ...his.nc D-Hydro output file
        outputdir  : absolute windows directory path to the location newly
        created csv files

        Returns
        -------
        HisResults object with lists of objects retrieved from his file
==========================================================================="""
# TODO: create function in hisreader that returns a dict of ObservationPoint(s)
# {"name": ObservationPoint}


class ExtStructure(Structure):
    Measured: Optional[PandasDataFrame]
    Simulated: PandasDataFrame

    def default_plot(self, dfs: List, variable: str, labels: List = None) -> None:
        plt.figure(figsize=(12, 4))
        for ix, df in enumerate(dfs):
            if labels is None:
                plt.plot(df[variable].dropna())
            else:
                plt.plot(df[variable].dropna(), label=labels[ix])

        if labels is not None:
            plt.legend()

        plt.gca().update(
            dict(title=r"Plot of: " + variable, xlabel="date", ylabel=variable)
        )
        plt.grid()
        plt.show()

    def simulated_plot(self, variable: str) -> None:
        self.default_plot(dfs=[self.Simulated], variable=variable)

    def measured_plot(self, variable: str) -> None:
        self.default_plot(dfs=[self.Measured], variable=variable)

    def measured_vs_simulated_plot(self, variable: str) -> None:
        self.default_plot(
            dfs=[self.Simulated, self.Measured],
            variable=variable,
            labels=["Simulated " + variable, "Measured " + variable],
        )


class ObservationPoint(ExtStructure):
    """Data model for observation points"""

    type: Literal["observation point"] = Field("observation point")


class Weir(Structure):
    """Data model for weirs"""

    type: Literal["weir"] = Field("weir")


## Define new structures here


class HisResults(object):
    def __init__(self, inputdir: str, outputdir: str) -> None:
        # TODO: let user choose what to execute
        self.__inputdir = inputdir
        self.__outputdir = outputdir
        self.__read_netcdf()
        self.__read_netcdf_variables()
        self.__make_timeframe()
        self.__parse_observation_points()
        self.__parse_weirs()
        del self.__ds

    def __read_netcdf(self) -> None:
        fnin = list(pathlib.Path(self.__inputdir).glob("*his.nc"))
        self.__ds = uf.DatasetUG(fnin[0], "r")
        self.variables = self.__ds.variables

    def __read_netcdf_variables(self) -> None:
        for key in self.__ds.variables.keys():
            if "_id" in key:
                setattr(
                    self, key, np.array(nc.chartostring(self.__ds.variables[key][:, :]))
                )

            else:
                setattr(self, key, self.__ds.variables[key])

    def __make_timeframe(self) -> None:
        t_start = (
            self.__ds.variables["time"]
            .units.split("since ")[1]
            .replace("+00:00", "")
            .strip()
        )
        start_datetime = dt.datetime.fromisoformat(t_start)
        end_datetime = start_datetime + dt.timedelta(
            seconds=np.amax(self.__ds.variables["time"][:])
        )
        delta_time = self.__ds.variables["time"][1] - self.__ds.variables["time"][0]
        df = (
            pd.date_range(
                start_datetime,
                end_datetime,
                freq="{}S".format(delta_time),
            )
            .to_frame(name="time")
            .set_index("time")
        )
        self.timeframe = df

    def __parse_structures(
        self, structure_type: str, structure_obj: Type[Structure], variables: List
    ) -> List:
        """General function to parse structures from His files"""
        structure_list = []
        structure_names = np.array(
            nc.chartostring(self.__ds.variables[structure_type + r"_id"][:, :])
        )
        for struct_ix in range(len(structure_names)):
            data = self.timeframe.copy()
            for variable in variables:
                data[variable] = self.__ds.variables[variable][:, struct_ix]

            name = structure_names[struct_ix]
            xcoordinate = self.__ds.variables[structure_type + r"_geom_node_coordx"][
                struct_ix
            ]
            ycoordinate = self.__ds.variables[structure_type + r"_geom_node_coordy"][
                struct_ix
            ]

            structure = structure_obj(
                id=name,
                name=name,
                numcoordinates=2,
                xcoordinates=[xcoordinate, xcoordinate],
                ycoordinates=[ycoordinate, ycoordinate],
                Measured=self.timeframe.copy(),
                Simulated=data,
            )
            structure_list.append(structure)
        return structure_list

    def __parse_observation_points(
        self, variables: List = ["waterlevel", "waterdepth", "discharge_magnitude"]
    ) -> None:
        """Specific function to parse observation points from His file"""
        if "station_id" in self.__ds.variables:
            self.observation_points = self.__parse_structures(
                structure_type="station",
                structure_obj=ObservationPoint,
                variables=variables,
            )

    def __parse_weirs(
        self, variables: List = ["weirgen_crest_level", "weirgen_s1up", "weirgen_s1dn"]
    ) -> None:
        """Specific function to parse weirs from His file"""
        if "weirgen_id" in self.__ds.variables:
            self.weirs = self.__parse_structures(
                structure_type="weirgen", structure_obj=Weir, variables=variables
            )

    # Define new structure parsers here
