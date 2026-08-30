"""
Core data processing pipeline for WCGBTS bathymetric data.
Parses Simrad EK60/EK80 raw data files, extracts spatial positions (including optional
external NMEA logs), calculates or extracts bottom depth picks, and exports
spatial bathymetry datasets in standard hydrographic GIS formats.
"""

import os
# Self-healing environment fix for PROJ database path collision on Windows
try:
    import rasterio
    rasterio_dir = os.path.dirname(rasterio.__file__)
    proj_data_path = os.path.join(rasterio_dir, "proj_data")
    if os.path.isdir(proj_data_path):
        os.environ["PROJ_DATA"] = proj_data_path
except Exception:
    pass

import re
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import xarray as xr
import geopandas as gpd
import echopype as ep
from echopype.echodata.echodata import EchoData
from shapely.geometry import Point
import h5py
import rasterio
from rasterio.transform import from_origin


def parse_nmea_coord(val: str, direction: str) -> float:
    """
    Parse NMEA coordinates in ddmm.mmmm format to decimal degrees.

    Parameters
    ----------
    val : str
        NMEA coordinate string (e.g., '4438.7534' or '12426.4158')
    direction : str
        Cardinal direction ('N', 'S', 'E', 'W')

    Returns
    -------
    float
        Decimal degrees, or np.nan if parsing fails.
    """
    if not val or not isinstance(val, str) or not direction:
        return np.nan
    try:
        dot_idx = val.find('.')
        if dot_idx == -1:
            minutes_str = val[-2:]
            degrees_str = val[:-2]
        else:
            minutes_str = val[dot_idx - 2:]
            degrees_str = val[:dot_idx - 2]

        degrees = float(degrees_str) if degrees_str else 0.0
        minutes = float(minutes_str) if minutes_str else 0.0
        deg = degrees + minutes / 60.0

        if direction.upper() in ["S", "W"]:
            deg = -deg
        return deg
    except ValueError:
        return np.nan


def parse_external_nmea(file_path: str, default_date: datetime = None) -> pd.DataFrame:
    """
    Parse external NMEA logs containing GPRMC or GPGGA sentences.

    Parameters
    ----------
    file_path : str
        Path to the external NMEA log file.
    default_date : datetime, optional
        A datetime object supplying the date for GPGGA sentences which
        do not contain a date component. If None, GGA sentences will
        be ignored until an RMC sentence with a date is parsed.

    Returns
    -------
    pd.DataFrame
        DataFrame with columns ['timestamp', 'latitude', 'longitude']
    """
    records = []
    current_date_str = None
    if default_date is not None:
        current_date_str = default_date.strftime("%d%m%y")

    with open(file_path, "r", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("$"):
                continue
            # Strip NMEA checksum if present
            if "*" in line:
                line = line.split("*")[0]
            parts = line.split(",")
            if not parts:
                continue

            sentence_id = parts[0]
            if sentence_id.endswith("RMC"):
                # GPRMC format: $GPRMC,time,status,lat,N/S,lon,E/W,speed,track,date,...
                if len(parts) >= 10:
                    time_str = parts[1]
                    status = parts[2]
                    lat_str = parts[3]
                    ns = parts[4]
                    lon_str = parts[5]
                    ew = parts[6]
                    date_str = parts[9]

                    if status == "A" and lat_str and lon_str:
                        lat = parse_nmea_coord(lat_str, ns)
                        lon = parse_nmea_coord(lon_str, ew)
                        if not np.isnan(lat) and not np.isnan(lon):
                            try:
                                dt = datetime.strptime(f"{date_str} {time_str.split('.')[0]}", "%d%m%y %H%M%S")
                                if "." in time_str:
                                    ms = int(float(f"0.{time_str.split('.')[1]}") * 1000000)
                                    dt = dt + timedelta(microseconds=ms)
                                records.append({"timestamp": dt, "latitude": lat, "longitude": lon})
                                current_date_str = date_str
                            except ValueError:
                                pass

            elif sentence_id.endswith("GGA"):
                # GPGGA format: $GPGGA,time,lat,N/S,lon,E/W,fix_quality,...
                if len(parts) >= 6:
                    time_str = parts[1]
                    lat_str = parts[2]
                    ns = parts[3]
                    lon_str = parts[4]
                    ew = parts[5]
                    fix_quality = parts[6] if len(parts) > 6 else "1"

                    if fix_quality != "0" and lat_str and lon_str:
                        lat = parse_nmea_coord(lat_str, ns)
                        lon = parse_nmea_coord(lon_str, ew)
                        if not np.isnan(lat) and not np.isnan(lon) and current_date_str:
                            try:
                                dt = datetime.strptime(f"{current_date_str} {time_str.split('.')[0]}", "%d%m%y %H%M%S")
                                if "." in time_str:
                                    ms = int(float(f"0.{time_str.split('.')[1]}") * 1000000)
                                    dt = dt + timedelta(microseconds=ms)
                                records.append({"timestamp": dt, "latitude": lat, "longitude": lon})
                            except ValueError:
                                pass

    if not records:
        return pd.DataFrame(columns=["timestamp", "latitude", "longitude"])
    df = pd.DataFrame(records)
    df = df.sort_values("timestamp").drop_duplicates("timestamp")
    return df


def interpolate_positions(ping_times, nmea_df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """
    Interpolate spatial coordinates to align with acoustic ping times.
    Uses linear interpolation and constant-value boundary extrapolation.

    Parameters
    ----------
    ping_times : array-like of datetime64
        Target timestamps for the echosounder pings.
    nmea_df : pd.DataFrame
        DataFrame of parsed positions containing ['timestamp', 'latitude', 'longitude']

    Returns
    -------
    tuple of (latitudes, longitudes) as numpy arrays.
    """
    if nmea_df.empty:
        raise ValueError("Cannot interpolate positions from an empty dataframe.")

    if len(nmea_df) == 1:
        lat_val = nmea_df.iloc[0]["latitude"]
        lon_val = nmea_df.iloc[0]["longitude"]
        return np.full_like(ping_times, lat_val, dtype=float), np.full_like(ping_times, lon_val, dtype=float)

    # Ensure everything is in numeric format (nanoseconds since epoch) for np.interp
    nmea_times = pd.to_datetime(nmea_df["timestamp"]).values.astype(np.float64)
    ping_times_numeric = pd.to_datetime(ping_times).values.astype(np.float64)

    lats = nmea_df["latitude"].values
    lons = nmea_df["longitude"].values

    # Perform interpolation with np.interp (performs constant-value extrapolation outside boundary)
    lat_interp = np.interp(ping_times_numeric, nmea_times, lats)
    lon_interp = np.interp(ping_times_numeric, nmea_times, lons)

    return lat_interp, lon_interp


def extract_embedded_positions(ed: EchoData, ping_times) -> tuple[np.ndarray, np.ndarray]:
    """
    Extract embedded positions and timestamps from the EchoData.Platform group,
    and interpolate them to match the acoustic ping times.

    Parameters
    ----------
    ed : EchoData
        EchoData object parsed from the Simrad raw file.
    ping_times : array-like of datetime64
        Acoustic ping times.

    Returns
    -------
    tuple of (latitudes, longitudes) as numpy arrays.
    """
    if "Platform" not in ed.group_map and not hasattr(ed, "platform"):
        return np.full_like(ping_times, np.nan, dtype=float), np.full_like(ping_times, np.nan, dtype=float)

    plat = ed["Platform"]
    if "latitude" not in plat.data_vars or "longitude" not in plat.data_vars or len(plat.time1) == 0:
        return np.full_like(ping_times, np.nan, dtype=float), np.full_like(ping_times, np.nan, dtype=float)

    # Build a clean dataframe from embedded platform position points
    nmea_df = pd.DataFrame({
        "timestamp": pd.to_datetime(plat.time1.values),
        "latitude": plat.latitude.values,
        "longitude": plat.longitude.values
    }).dropna(subset=["latitude", "longitude"])

    if nmea_df.empty:
        return np.full_like(ping_times, np.nan, dtype=float), np.full_like(ping_times, np.nan, dtype=float)

    return interpolate_positions(ping_times, nmea_df)


def extract_bottom_depth_picks(ed: EchoData) -> np.ndarray:
    """
    Extract the echosounder's bottom depth picks from the EchoData object.
    Falls back to a peak backscatter intensity tracking algorithm if proprietary
    bottom picks are not found in the dataset.

    Parameters
    ----------
    ed : EchoData
        EchoData object parsed from the Simrad raw file.

    Returns
    -------
    np.ndarray
        Array of bottom depth picks in meters.
    """
    # 1. Attempt to find seafloor detection in vendor specific dataset (added via BOT files)
    if "vendor" in ed.group_map:
        vendor_ds = ed.vendor
        if vendor_ds is not None and "detected_seafloor_depth" in vendor_ds.data_vars:
            depth_da = vendor_ds["detected_seafloor_depth"]
            if "channel" in depth_da.dims:
                depth_da = depth_da.isel(channel=0)
            pings = ed["Sonar/Beam_group1"].ping_time
            if "ping_time" in depth_da.dims:
                # Interpolate to match sonar beam ping times
                depth_vals = depth_da.interp(ping_time=pings, kwargs={"fill_value": "extrapolate"}).values
                return np.array(depth_vals)

    # 2. Fallback: Custom Peak Backscatter Power bottom detector
    bg = ed["Sonar/Beam_group1"]
    backscatter_da = bg.backscatter_r
    if "channel" in backscatter_da.dims:
        backscatter_da = backscatter_da.isel(channel=0)

    # Obtain sample intervals per ping
    sample_interval_da = bg.sample_interval
    if "channel" in sample_interval_da.dims:
        sample_interval_da = sample_interval_da.isel(channel=0)
    sample_intervals = sample_interval_da.values

    # Extract indicative sound speed from environmental variables
    sound_speed = 1485.0
    if "Environment" in ed.group_map and ed["Environment"] is not None:
        env = ed["Environment"]
        if "sound_speed_indicative" in env.data_vars:
            sound_speed = float(env.sound_speed_indicative.values[0])
        elif "transducer_sound_speed" in env.data_vars:
            sound_speed = float(env.transducer_sound_speed.values[0])

    backscatter_vals = backscatter_da.values  # Shape: (ping_time, range_sample)
    num_pings, num_samples = backscatter_vals.shape

    # Skip near-surface acoustic returns (transducer ringing / bubbles)
    # Default is 200 bins. If sample vector is short, skip at most 25% of the samples.
    bin_skip = min(200, num_samples // 4) if num_samples > 4 else 0

    depths = []
    for idx in range(num_pings):
        ping_data = backscatter_vals[idx]
        if len(ping_data) > bin_skip:
            idx_max = np.argmax(ping_data[bin_skip:]) + bin_skip
        else:
            idx_max = np.argmax(ping_data)

        # Get sample interval for this ping
        if isinstance(sample_intervals, np.ndarray) and sample_intervals.ndim > 0:
            s_interval = sample_intervals[idx]
        else:
            s_interval = sample_intervals

        # Echosounder depth = index * interval * speed of sound / 2 (two-way travel time)
        depth_m = idx_max * s_interval * sound_speed / 2.0
        depths.append(depth_m)

    return np.array(depths)


def export_to_cog(gdf: gpd.GeoDataFrame, output_path: str, grid_res_m: float = 50.0):
    """
    Rasterize GeoDataFrame points into a regular grid and export as a Cloud
    Optimized GeoTIFF (COG) in EPSG:3857 coordinates.

    Parameters
    ----------
    gdf : geopandas.GeoDataFrame
        The spatial GeoDataFrame in EPSG:4326.
    output_path : str
        Output file path for the .tif/.tiff file.
    grid_res_m : float
        Grid resolution in meters (default 50m).
    """
    if gdf.empty:
        raise ValueError("Cannot export empty GeoDataFrame to COG.")

    # Project to Web Mercator (EPSG:3857) to grid in metric units
    gdf_proj = gdf.to_crs(epsg=3857)
    xs = gdf_proj.geometry.x.values
    ys = gdf_proj.geometry.y.values
    depths = gdf_proj["depth"].values

    # Define bounds and add padding
    xmin, ymin, xmax, ymax = xs.min(), ys.min(), xs.max(), ys.max()
    padding = grid_res_m * 2
    xmin -= padding
    xmax += padding
    ymin -= padding
    ymax += padding

    # Create grid geometry
    width = int(np.ceil((xmax - xmin) / grid_res_m))
    height = int(np.ceil((ymax - ymin) / grid_res_m))

    if width <= 0 or height <= 0:
        raise ValueError("Dataset spatial footprint is too small for the specified grid resolution.")

    grid_data = np.full((height, width), np.nan, dtype=np.float32)
    grid_counts = np.zeros((height, width), dtype=np.int32)

    # Bin points to cells
    cols = ((xs - xmin) / grid_res_m).astype(int)
    rows = ((ymax - ys) / grid_res_m).astype(int)  # invert y-axis for standard raster structure

    valid_idx = (cols >= 0) & (cols < width) & (rows >= 0) & (rows < height)
    cols = cols[valid_idx]
    rows = rows[valid_idx]
    depths_valid = depths[valid_idx]

    # Accumulate depths and counts
    for r, c, d in zip(rows, cols, depths_valid):
        if np.isnan(grid_data[r, c]):
            grid_data[r, c] = 0.0
        grid_data[r, c] += d
        grid_counts[r, c] += 1

    # Divide by count to get mean depth per cell
    mask = grid_counts > 0
    grid_data[mask] /= grid_counts[mask]
    grid_data[~mask] = -9999.0  # NoData value

    # Affine transform: origin is top-left corner
    transform = from_origin(xmin, ymax, grid_res_m, grid_res_m)

    # Write as a tiled and compressed GeoTIFF (Cloud Optimized layout)
    with rasterio.open(
        output_path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype=rasterio.float32,
        crs="EPSG:3857",
        transform=transform,
        nodata=-9999.0,
        tiled=True,
        blockxsize=256,
        blockysize=256,
        compress="deflate",
    ) as dst:
        dst.write(grid_data, 1)
        # Build overviews for quick zoom rendering in GIS (essential for Cloud Optimized GeoTIFF)
        overviews = [2, 4, 8, 16]
        dst.build_overviews(overviews, rasterio.enums.Resampling.average)
        dst.update_tags(ns="rio_overview", resampling="average")


def export_to_hdf5(gdf: gpd.GeoDataFrame, output_path: str):
    """
    Export GeoDataFrame to an HDF5 database layout.

    Parameters
    ----------
    gdf : geopandas.GeoDataFrame
        The spatial GeoDataFrame in EPSG:4326.
    output_path : str
        Output file path for the .h5/.hdf5 database.
    """
    if gdf.empty:
        raise ValueError("Cannot export empty GeoDataFrame to HDF5.")

    with h5py.File(output_path, "w") as f:
        # Convert pandas timestamps to string bytes
        times_encoded = gdf["timestamp"].astype(str).values.astype("S")
        f.create_dataset("timestamp", data=times_encoded, compression="gzip")
        f.create_dataset("longitude", data=gdf["longitude"].values, compression="gzip")
        f.create_dataset("latitude", data=gdf["latitude"].values, compression="gzip")
        f.create_dataset("depth", data=gdf["depth"].values, compression="gzip")
        
        # Set spatial attributes
        f.attrs["crs"] = gdf.crs.to_string() if gdf.crs else "EPSG:4326"
        f.attrs["count"] = len(gdf)


def process_bathy(raw_path: str, nmea_log_path: str = None, sonar_model: str = "EK80") -> gpd.GeoDataFrame:
    """
    Main pipeline entry point. Parses Simrad raw data, calculates bottom depth,
    georeferences pings, and outputs a georereferenced GeoDataFrame.

    Parameters
    ----------
    raw_path : str
        Path to the Simrad .raw file.
    nmea_log_path : str, optional
        Path to external NMEA log file.
    sonar_model : str
        Simrad model ("EK60" or "EK80"). Default is "EK80".

    Returns
    -------
    geopandas.GeoDataFrame
        Spatial DataFrame containing ['timestamp', 'longitude', 'latitude', 'depth']
    """
    if not os.path.exists(raw_path):
        raise FileNotFoundError(f"Raw data file not found: {raw_path}")

    # 1. Parse Simrad file
    ed = ep.open_raw(raw_path, sonar_model=sonar_model)

    # 2. Extract ping timestamps
    bg = ed["Sonar/Beam_group1"]
    ping_times = bg.ping_time.values

    # 3. Extract depth picks
    depth_picks = extract_bottom_depth_picks(ed)

    # 4. Ingest and interpolate positions (External has precedence over internal)
    if nmea_log_path is not None and os.path.exists(nmea_log_path):
        first_ping_dt = pd.to_datetime(ping_times[0]).to_pydatetime() if len(ping_times) > 0 else None
        external_df = parse_external_nmea(nmea_log_path, default_date=first_ping_dt)
        if not external_df.empty:
            latitude_vals, longitude_vals = interpolate_positions(ping_times, external_df)
        else:
            latitude_vals, longitude_vals = extract_embedded_positions(ed, ping_times)
    else:
        latitude_vals, longitude_vals = extract_embedded_positions(ed, ping_times)

    # 5. Assemble and format GeoDataFrame
    gdf = gpd.GeoDataFrame(
        {
            "timestamp": pd.to_datetime(ping_times),
            "longitude": longitude_vals,
            "latitude": latitude_vals,
            "depth": depth_picks,
        },
        geometry=gpd.points_from_xy(longitude_vals, latitude_vals),
        crs="EPSG:4326",
    )
    return gdf


def generate_echogram(raw_path: str, sonar_model: str = "EK80") -> xr.Dataset:
    """
    Generate Volume Backscattering Strength (Sv) dataset from a Simrad .raw file
    using echopype, with robust handling for NumPy 2.x compatibility issues.

    Parameters
    ----------
    raw_path : str
        Path to the Simrad .raw file.
    sonar_model : str
        Simrad model ("EK60" or "EK80"). Default is "EK80".

    Returns
    -------
    xarray.Dataset
        Dataset containing calculated Volume Backscattering Strength (Sv) data.
    """
    if not os.path.exists(raw_path):
        raise FileNotFoundError(f"Raw data file not found: {raw_path}")

    # 1. Parse Simrad file
    ed = ep.open_raw(raw_path, sonar_model=sonar_model)

    # 2. Self-healing workaround for NumPy 2.x compatibility with echopype 0.11.x
    # When filter_time in Vendor_specific is empty, it defaults to float64, while
    # ping_time has datetime64[ns] dtype. NumPy 2.0+ strictly forbids the intersection of
    # datetime64 and float64 dtypes, raising DTypePromotionError.
    # To bypass this, we drop variables that depend on filter_time and assign a single
    # dummy datetime64 value to filter_time coordinate.
    if "Vendor_specific" in ed._tree:
        vs_node = ed._tree["Vendor_specific"]
        if vs_node.ds is not None and "filter_time" in vs_node.ds.coords:
            if len(vs_node.ds["filter_time"]) == 0:
                vs = vs_node.ds
                vars_to_drop = [v for v in vs.data_vars if "filter_time" in vs[v].dims]
                vs_dropped = vs.drop_vars(vars_to_drop)
                # Assign a dummy filter_time of datetime64[ns] so that length is exactly 1
                dummy_time = np.array([pd.Timestamp("2018-06-01").to_datetime64()])
                vs_new = vs_dropped.assign_coords(filter_time=dummy_time)
                vs_node.ds = vs_new

    # 3. Compute Sv
    if sonar_model == "EK80":
        # Simrad EK80 requires explicit waveform and encode modes
        ds_Sv = ep.calibrate.compute_Sv(ed, waveform_mode="CW", encode_mode="power")
    else:
        # EK60 has default modes
        ds_Sv = ep.calibrate.compute_Sv(ed)

    return ds_Sv
