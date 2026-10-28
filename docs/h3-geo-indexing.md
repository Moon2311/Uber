# H3 Geographic Indexing (Step 3)

Each driver's latest location carries the [H3](https://h3geo.org) cell that
contains it. Candidate search starts at the pickup's cell and expands ring by
ring, stopping as soon as the answer is provably complete.

## ⚠️ Grid distance is not road distance or travel time

H3 **grid distance** is the number of hexagon hops between two cells. It says
nothing about streets.

- A driver 1 ring away may be across a river, on the far side of a motorway with
  no exit, or on a one-way street heading the wrong way. Their real drive can be
  many times longer than one that is 3 rings away.
- Hexagon size changes with latitude and is distorted near the 12 pentagons, so
  "k rings" isn't even a fixed straight-line distance.
- Our `distance_km` is **great-circle (straight-line)** distance. It is also not
  road distance.

In this codebase, grid distance is used **only** to bound and order the search,
meaning which cells to read and when to stop. Candidates are ranked by
straight-line distance as a placeholder. Dispatch and ETA decisions in later steps
must use a routing engine's ETA, computed for the short list of candidates this
search returns.

## Layering

```
apps/geoindex/h3_index.py   H3GeoIndex      the only module that imports h3
apps/geoindex/service.py    GeoSearchService pickup -> center cell, neighbours, ring steps
apps/locations/             LocationService  stores the cell with each fix; find_in_cells()
apps/drivers/candidates.py  find_candidate_drivers  availability rules + expansion policy
apps/drivers/services.py    find_nearby_drivers     public nearby API (unchanged shape)
```

Nothing above `apps/geoindex` knows the resolution. Changing `H3_RESOLUTION` is a
configuration change plus a reindex; no code changes are needed in candidate
retrieval or the future matching engine. `Candidate` exposes only `distance_km`
and `grid_distance`.

## Write path

`LocationService.record_driver_update` calls
`latlng_to_cell(lat, lng, H3_RESOLUTION)` and stores `h3_cell` and
`h3_resolution` in the same guarded UPSERT as the coordinates. An out-of-order
fix therefore can't change the cell either.

- Coordinates are range-checked **before** they reach H3. h3-py silently wraps
  `lat=95` into a valid cell instead of raising an error.
- If H3 fails, the fix is **still stored** without a cell and the
  `location.h3.index_error` metric is emitted. A GPS update is never lost because
  of indexing. The row is invisible to H3 search until the driver's next fix,
  which normally arrives within seconds.

## Geographic service

```python
geo = get_geo_search_service()                  # at settings.H3_RESOLUTION
area = geo.search_area(lat, lng, k=1)           # SearchArea(center_cell, neighbors, rings)
for step in geo.expanding_rings(lat, lng, max_k=geo.max_rings_for_radius(lat, lng, 5)):
    step.k, step.cells, step.min_distance_km    # ring k and a distance lower bound
```

## Bounded expansion (exact nearest-N)

`find_candidate_drivers(lat, lng, radius_km, category, limit)` works like this:

1. `max_k = rings_to_cover(center, radius_km)` is the smallest k guaranteed to
   cover every point within the radius of *any* point in the pickup cell. The
   bound comes from hex geometry: cells k hops apart have centers at least
   1.5·edge·k apart. The edge is measured from the actual cell's area and padded
   by 25% to absorb distortion. If `max_k > H3_MAX_GRID_DISTANCE` (default 40),
   the search falls back to bbox.
2. For k = 0, 1, 2, … each ring's cells are read through
   `LocationService.find_in_cells`, which only returns fresh fixes. The search
   then keeps drivers within `radius_km` (straight line) who pass the
   availability rules: ONLINE, an active vehicle, a matching `category` if given,
   and not on an active trip.
3. The search stops when either condition holds:
   - `limit` candidates are found **and** `min_distance_km_at(k)` (a lower bound
     on how close anything in ring k or beyond can be) is at least the current
     `limit`-th distance. Nothing unseen can be closer.
   - The ring's lower bound exceeds `radius_km`.

**Boundary cases:**
- **Ring 1 is always scanned.** A driver 20 m from the pickup can sit across a
  cell edge, so the center cell alone is never trusted.
- **Antimeridian.** H3 handles it natively: a driver at lng −179.9995 is found
  from a pickup at 179.9995. The retained bbox fallback does **not** handle this,
  and a test pins that behaviour.
- **Pentagons.** Rings around a pentagon have fewer cells. If `grid_ring` fails,
  `ring(k)` falls back to `disk(k) − disk(k−1)`, which is always correct.
- **Errors.** Every `h3.H3BaseException`, `ValueError` and `TypeError` from h3 is
  wrapped as `GeoIndexError`. In candidate search that triggers the bbox fallback
  and the `drivers.candidates.fallback{reason=h3_error}` metric.

## Fallback (retained)

The Step 1/2 bounding-box + haversine search is kept unchanged:

- `NEARBY_SEARCH_STRATEGY=bbox` switches back to it entirely.
- With `h3` (the default), it is used automatically on H3 errors or for radii
  that would need more than `H3_MAX_GRID_DISTANCE` rings.

Verification so far:
- `test_h3_matches_bbox_on_random_fleet` places 60 random drivers. For 6 pickups
  × 3 radius/limit/category combinations at resolutions 7, 8 and 9, it asserts
  that both strategies return **identical, identically ordered** results.
- The `apps.drivers` nearby API tests run against both strategies.

Keep the fallback until H3 has also been checked against real traffic, for
example by comparing results in shadow mode during rollout.

## Resolution

| Res | Avg edge | Avg area | Rings for 5 km radius* |
|---|---|---|---|
| 7 | 1.41 km | 5.16 km² | ~5 |
| **8 (default)** | 0.53 km | 0.74 km² | ~10 |
| 9 | 0.20 km | 0.11 km² | ~23 |

\* Computed by `rings_to_cover` in San Francisco. It is an upper bound; expansion usually stops much earlier.

Finer cells mean more, smaller lookups; coarser cells mean fewer lookups that
each return more rows to filter.

To change the resolution:
1. Set `H3_RESOLUTION`.
2. Deploy.
3. Run `make reindex-locations` (`manage.py reindex_driver_locations`).

Until the reindex runs, rows indexed at the old resolution don't match and those
drivers are missing from H3 search. They also self-heal on their next GPS fix,
usually within seconds. A test covers this.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `H3_RESOLUTION` | 8 | Resolution of stored cells (0–15; invalid → `ImproperlyConfigured`) |
| `H3_MAX_GRID_DISTANCE` | 40 | Ring cap; larger searches use bbox |
| `NEARBY_SEARCH_STRATEGY` | `h3` | `h3` or `bbox` |

## Metrics

| Metric | Type | Notes |
|---|---|---|
| `drivers.candidates.search` | counter | tag `strategy` = the strategy actually used |
| `drivers.candidates.fallback` | counter | tag `reason` = `h3_error` / `radius_exceeds_max_grid_distance` |
| `drivers.candidates.rings_scanned` | gauge | rings read in the last H3 search |
| `drivers.candidates.results` | gauge | — |
| `location.h3.index_error` | counter | fixes stored without a cell |

## Storage of cells (Step 4)

Cell membership now lives in Redis as one sorted set per cell, scored by the fix
timestamp (`P:cell:<h3>`). A driver's move between cells happens in the same Lua
script as the location write. Candidate retrieval still only calls
`LocationService.find_in_cells`, so this file's expansion policy didn't change.
See [redis-hot-store.md](redis-hot-store.md).
