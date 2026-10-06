# ng911topo

Find response-area boundaries that overlap, ground that no area covers, and roads that cross a boundary without being split. Tell a coverage gap from an enclave drawn on purpose.

> **Status: started, not built.** This repo holds the plan only. Nothing here runs yet.

## The disaster

Two response areas overlap by a sliver, or leave a strip that belongs to neither. A 911 call from inside that strip routes to the wrong agency, or to no agency. Most layers look clean, because the slivers are too thin to see. A sliver filter that drops digitizing noise can also drop a real gap, if its threshold is set without a look at what it removed.

## The plan

The port goes from arcpy to `shapely` and `pyproj`. This tool will need those two libraries: it is an exception to the standard-library-only rule of the other tools.

- Overlaps between polygons that should not overlap.
- Gaps, with deliberate enclaves told apart from coverage holes.
- A sliver-width gate. It reports its threshold, what it suppressed, and the nearest case just above the threshold.
- Roads that cross a boundary without a split.
- An explicit EPSG on every input. The tool never assumes a coordinate system. It reprojects to a stated metre CRS, and it proves the geometry was reprojected, not just re-tagged.

The work starts with one check, RA-04, and its seven tests. If that check goes green within a day, the rest follows.

## Related

- ng911check (private, in progress) runs the attribute and centerline checks.
- [sidestamp](https://github.com/uhsear/sidestamp) checks the values stamped on each side of a centerline against the polygon that side fronts.
- [ringwind](https://github.com/uhsear/ringwind) names GeoJSON rings wound the wrong way.
