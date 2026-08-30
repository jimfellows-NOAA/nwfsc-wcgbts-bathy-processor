"""
CLI Interface for WCGBTS Bathymetry Data Processor.
Provides command-line commands for data pipeline execution and spatial visualization.
"""

import argparse
import os
import sys
import geopandas as gpd
import pandas as pd
import h5py
import folium
import matplotlib.pyplot as plt
from nwfsc_wcgbts_bathy_processor.core import (
    process_bathy,
    export_to_cog,
    export_to_hdf5,
    generate_echogram,
)


def load_processed_dataset(file_path: str) -> gpd.GeoDataFrame:
    """
    Load a previously processed dataset from GeoParquet or HDF5 format.

    Parameters
    ----------
    file_path : str
        Path to the processed data file.

    Returns
    -------
    geopandas.GeoDataFrame
        Loaded georeferenced dataset.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Processed file not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()

    if ext in [".parquet", ".geoparquet"]:
        return gpd.read_parquet(file_path)
    elif ext in [".h5", ".hdf5"]:
        with h5py.File(file_path, "r") as f:
            lats = f["latitude"][:]
            lons = f["longitude"][:]
            depths = f["depth"][:]
            times = [t.decode("utf-8") for t in f["timestamp"][:]]
            
        gdf = gpd.GeoDataFrame(
            {
                "timestamp": pd.to_datetime(times),
                "longitude": lons,
                "latitude": lats,
                "depth": depths,
            },
            geometry=gpd.points_from_xy(lons, lats),
            crs="EPSG:4326",
        )
        return gdf
    else:
        raise ValueError(
            f"Unsupported file format '{ext}' for loading. "
            "Please provide a GeoParquet (.parquet) or HDF5 (.h5) file."
        )


def create_folium_map(gdf: gpd.GeoDataFrame, output_path: str):
    """
    Generate an interactive Folium HTML map showing the vessel track and sampled depth points.

    Parameters
    ----------
    gdf : geopandas.GeoDataFrame
        Georeferenced bathymetry points in EPSG:4326.
    output_path : str
        Output HTML file path.
    """
    if gdf.empty:
        raise ValueError("Cannot visualize empty GeoDataFrame.")

    # Drop any row with NaN coordinates
    gdf_clean = gdf.dropna(subset=["latitude", "longitude"])
    if gdf_clean.empty:
        raise ValueError("No valid coordinates available for spatial visualization.")

    mean_lat = gdf_clean["latitude"].mean()
    mean_lon = gdf_clean["longitude"].mean()

    # Initialize folium map
    m = folium.Map(location=[mean_lat, mean_lon], zoom_start=12, tiles="OpenStreetMap")

    # Plot vessel path
    coords = list(zip(gdf_clean["latitude"], gdf_clean["longitude"]))
    folium.PolyLine(
        locations=coords,
        color="darkblue",
        weight=4,
        opacity=0.8,
        tooltip="Vessel Path (Chronological)",
    ).add_to(m)

    # Plot start and end markers
    if len(coords) > 0:
        folium.Marker(
            location=coords[0],
            popup=f"Start: {gdf_clean['timestamp'].iloc[0]}",
            icon=folium.Icon(color="green", icon="play"),
        ).add_to(m)
        folium.Marker(
            location=coords[-1],
            popup=f"End: {gdf_clean['timestamp'].iloc[-1]}",
            icon=folium.Icon(color="red", icon="stop"),
        ).add_to(m)

    # Add interactive depth points (downsample to maximum 100 points for map responsiveness)
    step = max(1, len(gdf_clean) // 100)
    sampled = gdf_clean.iloc[::step]

    for _, row in sampled.iterrows():
        folium.CircleMarker(
            location=[row["latitude"], row["longitude"]],
            radius=4,
            color="red",
            fill=True,
            fill_color="yellow",
            fill_opacity=0.8,
            popup=f"Time (UTC): {row['timestamp']}<br>Depth: {row['depth']:.2f} m",
        ).add_to(m)

    m.save(output_path)


def create_depth_cross_section(gdf: gpd.GeoDataFrame, output_path: str):
    """
    Plot bathymetric depth cross-section over time as a PNG plot.

    Parameters
    ----------
    gdf : geopandas.GeoDataFrame
        The dataset containing timestamps and depth.
    output_path : str
        Output PNG file path.
    """
    if gdf.empty:
        raise ValueError("Cannot plot empty dataset.")

    gdf_sorted = gdf.sort_values("timestamp")

    plt.figure(figsize=(11, 5.5))
    plt.plot(gdf_sorted["timestamp"], gdf_sorted["depth"], color="navy", linewidth=1.5, label="Seafloor Bottom")
    plt.fill_between(
        gdf_sorted["timestamp"],
        gdf_sorted["depth"],
        color="skyblue",
        alpha=0.35,
        label="Water Column",
    )

    # Standard hydrographic practice: plot depth descending (inverted y-axis)
    plt.gca().invert_yaxis()

    plt.title("WCGBTS Echosounder Bathymetric Profile (Depth over Time)", fontsize=13, fontweight="bold")
    plt.xlabel("Time (UTC)", fontsize=11)
    plt.ylabel("Depth (meters)", fontsize=11)
    plt.grid(True, linestyle=":", alpha=0.6)
    plt.xticks(rotation=35)
    plt.legend(loc="upper right")
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()


def main():
    """
    Main entry point for command line execution.
    """
    parser = argparse.ArgumentParser(
        description="NWFSC WCGBTS Bathymetry Processor CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="Sub-commands")

    # Command: process
    parser_proc = subparsers.add_parser("process", help="Parse Simrad .raw files and extract positions & depths")
    parser_proc.add_argument("input_raw", help="Path to input Simrad .raw file")
    parser_proc.add_argument("--nmea-log", help="Path to optional external NMEA log file for position override")
    parser_proc.add_argument("--output", required=True, help="Path to save processed file (.parquet, .geoparquet, .h5, .hdf5, .tif, .tiff)")
    parser_proc.add_argument("--sonar-model", default="EK80", choices=["EK60", "EK80"], help="Simrad sonar model")

    # Command: visualize
    parser_vis = subparsers.add_parser("visualize", help="Generate vessel path HTML map and depth cross-section PNG")
    parser_vis.add_argument("processed_file", help="Path to previously processed GeoParquet (.parquet) or HDF5 (.h5) file")
    parser_vis.add_argument("--map-output", default="vessel_path.html", help="Path to output interactive map HTML")
    parser_vis.add_argument("--plot-output", default="depth_profile.png", help="Path to output depth profile cross-section PNG")

    # Command: echogram
    parser_echo = subparsers.add_parser("echogram", help="Generate Volume Backscattering Strength (Sv) water-column profile")
    parser_echo.add_argument("input_raw", help="Path to input Simrad .raw file")
    parser_echo.add_argument("--output", required=True, help="Path to save processed echogram (.zarr, .nc, .png, .jpg, .jpeg)")
    parser_echo.add_argument("--sonar-model", default="EK80", choices=["EK60", "EK80"], help="Simrad sonar model")

    args = parser.parse_args()

    try:
        if args.command == "process":
            print(f"Parsing Simrad raw file: {args.input_raw}")
            print(f"Sonar model specified: {args.sonar_model}")
            if args.nmea_log:
                print(f"Applying position injection override from NMEA log: {args.nmea_log}")

            # Execute pipeline
            gdf = process_bathy(args.input_raw, args.nmea_log, sonar_model=args.sonar_model)
            print(f"Pipeline parsed {len(gdf)} acoustic bottom pings successfully.")

            # Determine export format
            ext = os.path.splitext(args.output)[1].lower()
            if ext in [".parquet", ".geoparquet"]:
                print(f"Exporting to GeoParquet format: {args.output}")
                gdf.to_parquet(args.output)
            elif ext in [".h5", ".hdf5"]:
                print(f"Exporting to HDF5 format: {args.output}")
                export_to_hdf5(gdf, args.output)
            elif ext in [".tif", ".tiff"]:
                print(f"Exporting to raster Cloud Optimized GeoTIFF format (50m resolution): {args.output}")
                export_to_cog(gdf, args.output)
            else:
                raise ValueError(
                    f"Unsupported output file format '{ext}'. "
                    "Supported formats: GeoParquet (.parquet), HDF5 (.h5), GeoTIFF (.tif)"
                )
            print("Processing task completed successfully.")

        elif args.command == "visualize":
            print(f"Loading processed dataset: {args.processed_file}")
            gdf = load_processed_dataset(args.processed_file)

            print(f"Generating interactive track map: {args.map_output}")
            create_folium_map(gdf, args.map_output)

            print(f"Generating depth cross-section profile: {args.plot_output}")
            create_depth_cross_section(gdf, args.plot_output)

            print("Visualization task completed successfully.")

        elif args.command == "echogram":
            print(f"Generating echogram for Simrad raw file: {args.input_raw}")
            print(f"Sonar model specified: {args.sonar_model}")

            ds_Sv = generate_echogram(args.input_raw, sonar_model=args.sonar_model)
            print("Volume Backscattering Strength (Sv) calculated successfully.")

            ext = os.path.splitext(args.output)[1].lower()
            if ext == ".zarr":
                print(f"Exporting quantitative multi-dimensional dataset to Zarr: {args.output}")
                ds_Sv.to_zarr(args.output, mode="w")
            elif ext in [".nc", ".netcdf"]:
                print(f"Exporting legacy dataset to NetCDF: {args.output}")
                ds_Sv.to_netcdf(args.output)
            elif ext in [".png", ".jpg", ".jpeg"]:
                print(f"Generating 2D echogram visualization: {args.output}")
                if "Sv" not in ds_Sv.data_vars:
                    raise KeyError("Dataset does not contain 'Sv' variable.")

                sv_data = ds_Sv["Sv"].isel(channel=0)
                plt.figure(figsize=(11, 6))

                # Plot with ping_time on X, range_sample on Y, inverting Y-axis
                sv_data.plot(x="ping_time", y="range_sample", cmap="viridis", vmin=-80, vmax=-30)
                plt.gca().invert_yaxis()
                plt.title(f"Volume Backscattering Strength (Sv) - Channel 0", fontsize=12, fontweight="bold")
                plt.xlabel("Ping Time", fontsize=10)
                plt.ylabel("Range Sample Index", fontsize=10)
                plt.tight_layout()
                plt.savefig(args.output, dpi=150)
                plt.close()
            else:
                raise ValueError(
                    f"Unsupported output file format '{ext}' for echogram. "
                    "Supported formats: .zarr, .nc, .png, .jpg, .jpeg"
                )
            print("Echogram task completed successfully.")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
