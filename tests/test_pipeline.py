"""
Verbose pytest test suite for the WCGBTS bathymetric processor.
Covers coordinate parsing, external NMEA file parsing, interpolation,
and full-scale integration testing on local sample raw echosounder files.
"""

import os
import glob
from datetime import datetime
import numpy as np
import pandas as pd
import geopandas as gpd
import pytest
import h5py
import rasterio

from nwfsc_wcgbts_bathy_processor.core import (
    parse_nmea_coord,
    parse_external_nmea,
    interpolate_positions,
    process_bathy,
    export_to_cog,
    export_to_hdf5,
    generate_echogram,
)
from nwfsc_wcgbts_bathy_processor.cli import load_processed_dataset


def find_sample_raw_file() -> str:
    """Helper to locate a sample .raw file dynamically in data/raw/."""
    # Try current directory root search
    paths = glob.glob("data/raw/**/*.raw", recursive=True)
    if paths:
        return paths[0]
    
    # Try searching relative to this test file
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = glob.glob(os.path.join(parent_dir, "data/raw/**/*.raw"), recursive=True)
    if paths:
        return paths[0]
        
    return ""


# ==============================================================================
# UNIT TESTS
# ==============================================================================

@pytest.mark.parametrize(
    "val,direction,expected",
    [
        ("4438.7534", "N", 44.64589),
        ("12426.4158", "W", -124.440263),
        ("0000.0000", "N", 0.0),
        ("", "N", np.nan),
        (None, "S", np.nan),
        ("invalid", "E", np.nan),
    ],
)
def test_parse_nmea_coord(val, direction, expected):
    """Test NMEA angle coordinate conversion from ddmm.mmmm to decimal degrees."""
    result = parse_nmea_coord(val, direction)
    if np.isnan(expected):
        assert np.isnan(result)
    else:
        assert pytest.approx(result, abs=1e-6) == expected


def test_parse_external_nmea(tmp_path):
    """Test external NMEA log parsing with mocked RMC and GGA lines."""
    nmea_content = (
        "$GPRMC,183020,A,4438.7534,N,12426.4158,W,5.9,54.9,010618,16,E*52\n"
        "$GPGGA,183022,4438.7552,N,12426.4120,W,1,11,1.6,6,M,-22,M,,*6A\n"
        "Some invalid or unrelated comment line\n"
        "$GPRMC,183024,V,4438.7570,N,12426.4082,W,5.9,55.2,010618,16,E*5B\n"  # Status 'V' (Void) - should be ignored
    )
    nmea_file = tmp_path / "external_nmea.log"
    nmea_file.write_text(nmea_content)

    # 1. Parse without default date (date extracted from RMC)
    df = parse_external_nmea(str(nmea_file))
    assert len(df) == 2  # GPRMC (18:30:20) and GPGGA (18:30:22, dates inherited from previous RMC)
    
    # Assert values
    assert df.iloc[0]["timestamp"] == datetime(2018, 6, 1, 18, 30, 20)
    assert pytest.approx(df.iloc[0]["latitude"]) == 44.64589
    assert pytest.approx(df.iloc[0]["longitude"]) == -124.440263

    assert df.iloc[1]["timestamp"] == datetime(2018, 6, 1, 18, 30, 22)
    assert pytest.approx(df.iloc[1]["latitude"]) == 44.64592
    assert pytest.approx(df.iloc[1]["longitude"]) == -124.440200


def test_interpolate_positions():
    """Test interpolation of NMEA coordinates to match ping times."""
    # Set up NMEA positions (1 Hz sampling)
    nmea_data = pd.DataFrame(
        {
            "timestamp": [
                datetime(2018, 6, 1, 12, 0, 0),
                datetime(2018, 6, 1, 12, 0, 2),
            ],
            "latitude": [45.0, 45.1],
            "longitude": [-124.0, -124.2],
        }
    )

    # Set up acoustic ping timestamps (different timestamps, some out-of-bounds)
    ping_times = pd.to_datetime(
        [
            "2018-06-01 11:59:59",  # Out of bounds (under) -> should extrapolate
            "2018-06-01 12:00:00",  # Exact match
            "2018-06-01 12:00:01",  # Midpoint
            "2018-06-01 12:00:02",  # Exact match
            "2018-06-01 12:00:03",  # Out of bounds (over) -> should extrapolate
        ]
    ).values

    lats, lons = interpolate_positions(ping_times, nmea_data)

    assert len(lats) == 5
    assert len(lons) == 5

    # Match exact matches
    assert lats[1] == 45.0
    assert lats[3] == 45.1
    assert lons[1] == -124.0
    assert lons[3] == -124.2

    # Match midpoint interpolation
    assert pytest.approx(lats[2]) == 45.05
    assert pytest.approx(lons[2]) == -124.1

    # Match boundaries extrapolation (using constant-value boundaries or linear extrapolation)
    # np.interp uses constant extrapolation for boundary points
    assert lats[0] == 45.0
    assert lats[4] == 45.1


# ==============================================================================
# INTEGRATION & FILE FORMAT TESTS
# ==============================================================================

def test_integration_pipeline_execution(tmp_path):
    """
    Perform a complete integration test using local sample echosounder raw data.
    Verifies parsing, depth tracking, position interpolation, CRS setting,
    and GeoParquet, HDF5, and COG formats writing/loading.
    """
    sample_file = find_sample_raw_file()
    if not sample_file:
        pytest.skip("No sample Simrad .raw file found in data/raw/. Skipping integration test.")

    print(f"Running integration test on raw file: {sample_file}")

    # 1. Run pipeline
    gdf = process_bathy(sample_file, sonar_model="EK80")

    # 2. Assert spatial dataframe schema and completeness
    assert isinstance(gdf, gpd.GeoDataFrame)
    assert not gdf.empty
    assert len(gdf) > 100  # Our sample file has 324 pings

    # Assert columns
    for col in ["timestamp", "longitude", "latitude", "depth", "geometry"]:
        assert col in gdf.columns

    # Assert CRS setting (EPSG:4326)
    assert gdf.crs.to_string() == "EPSG:4326"

    # Assert logical geospatial boundaries matching Oregon/WCGBTS survey footprint
    assert gdf["latitude"].between(40.0, 49.0).all()
    assert gdf["longitude"].between(-126.0, -123.0).all()

    # Assert logical bottom depth measurements in meters
    assert (gdf["depth"] > 10.0).all()  # depth should be deeper than 10m
    assert (gdf["depth"] < 1500.0).all()  # depth should be shallower than 1500m

    # Assert geometries are correct types
    assert gdf.geometry.geom_type.eq("Point").all()

    # 3. Test export to GeoParquet and reading back
    parquet_path = tmp_path / "bathy.parquet"
    gdf.to_parquet(str(parquet_path))
    assert parquet_path.exists()

    gdf_loaded_pq = load_processed_dataset(str(parquet_path))
    assert isinstance(gdf_loaded_pq, gpd.GeoDataFrame)
    assert len(gdf_loaded_pq) == len(gdf)
    assert gdf_loaded_pq.crs.to_string() == "EPSG:4326"

    # 4. Test export to HDF5 and reading back
    hdf5_path = tmp_path / "bathy.h5"
    export_to_hdf5(gdf, str(hdf5_path))
    assert hdf5_path.exists()

    gdf_loaded_h5 = load_processed_dataset(str(hdf5_path))
    assert isinstance(gdf_loaded_h5, gpd.GeoDataFrame)
    assert len(gdf_loaded_h5) == len(gdf)
    assert gdf_loaded_h5.crs.to_string() == "EPSG:4326"
    assert pytest.approx(gdf_loaded_h5["depth"].values) == gdf["depth"].values

    # 5. Test export to Cloud Optimized GeoTIFF (COG) and validating structure
    tif_path = tmp_path / "bathy.tif"
    export_to_cog(gdf, str(tif_path), grid_res_m=100.0)
    assert tif_path.exists()

    # Load raster and inspect spatial metadata
    with rasterio.open(str(tif_path)) as src:
        assert src.crs.to_string() == "EPSG:3857"  # Grid coordinates projected to Web Mercator
        assert src.nodata == -9999.0
        assert src.count == 1
        # Assert that overviews are generated for COG structure
        assert len(src.overviews(1)) > 0


def test_nmea_injection_logic(tmp_path):
    """
    Test the optional NMEA position override injection logic.
    Mocks an external NMEA log string, processes the echosounder raw file
    with this log, and verifies that the interpolated positions match the injected path.
    """
    sample_file = find_sample_raw_file()
    if not sample_file:
        pytest.skip("No sample Simrad .raw file found in data/raw/. Skipping NMEA injection test.")

    # 1. Create a mocked external NMEA file with coordinates shifted from the embedded GPS track.
    # Embedded coordinates are around lat 44.64, lon -124.44.
    # We will inject coordinates shifted to lat 45.5, lon -125.5.
    nmea_lines = (
        "$GPRMC,183020,A,4530.0000,N,12530.0000,W,10.0,90.0,010618,16,E*52\n"
        "$GPRMC,183840,A,4531.0000,N,12531.0000,W,10.0,90.0,010618,16,E*52\n"
    )
    nmea_file = tmp_path / "external_overrides.nmea"
    nmea_file.write_text(nmea_lines)

    # 2. Run pipeline with the external log override
    gdf_override = process_bathy(sample_file, nmea_log_path=str(nmea_file), sonar_model="EK80")

    # 3. Assert that the resulting positions match our external overrides instead of the embedded track
    assert not gdf_override.empty
    assert gdf_override["latitude"].between(45.49, 45.52).all()
    assert gdf_override["longitude"].between(-125.52, -125.49).all()


def test_echogram_generation(tmp_path):
    """
    Test Volume Backscattering Strength (Sv) echogram generation.
    Saves a Zarr dataset and a PNG image, and validates that they are
    correctly created on disk and contain the Sv variable.
    """
    sample_file = find_sample_raw_file()
    if not sample_file:
        pytest.skip("No sample Simrad .raw file found in data/raw/. Skipping echogram test.")

    # 1. Generate Sv dataset
    ds_Sv = generate_echogram(sample_file, sonar_model="EK80")

    assert "Sv" in ds_Sv.data_vars
    assert ds_Sv["Sv"].ndim == 3  # (channel, ping_time, range_sample)

    # 2. Export to Zarr
    zarr_path = tmp_path / "echogram.zarr"
    ds_Sv.to_zarr(str(zarr_path), mode="w")
    assert zarr_path.exists()

    # 3. Export to PNG image (first channel)
    png_path = tmp_path / "echogram.png"
    sv_data = ds_Sv["Sv"].isel(channel=0)

    import matplotlib.pyplot as plt
    plt.figure(figsize=(10, 6))
    sv_data.plot(x="ping_time", y="range_sample", cmap="viridis", vmin=-80, vmax=-30)
    plt.gca().invert_yaxis()
    plt.title("Volume Backscattering Strength (Sv) - Test")
    plt.savefig(str(png_path))
    plt.close()

    assert png_path.exists()
