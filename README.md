# ng911topo

Find response-area polygons that overlap, enclosed ground that no polygon covers, and street ends
that stop short of another street. Every overlap and gap threshold is printed, together with what it suppressed and the
nearest case just above it. The tool refuses to run when a boundary layer it was told to use
cannot be trusted, instead of printing a flood of false findings.

A response-area layer shows no error where two areas overlap by a sliver, or leave a strip that
belongs to neither. Most of the layer looks clean, because the slivers are too thin to see. A 911
call from inside such a strip can route to the wrong agency, or to none.

The usual defence is a width filter that drops digitizing noise. That filter is a decision made
once, with a number, usually without a look at what it removed. A real gap 0.4 m wide goes out
with the cracks. Nobody sees it go, because the filter writes nothing about its own work.

The street layer has the same problem. A list of dangling ends holds a few hundred real dead
ends, and in the middle of them sit the ends that stop 7 m short of another road. They are not
dead ends. They are two roads that never joined, and a router cannot cross the gap. A flat list
hides them, because every row looks like every other row.

The worst day is the day the boundary layer does not load. The script that suppresses the road
ends at the edge of the map reads an empty layer, suppresses nothing, and prints every edge end as
a finding. The list is now so long that nobody reads it, and the next real finding is lost in it.
Two road ends 0.0002 m apart can do a smaller version of the same thing: a rounding grid puts
them in different cells, and one node becomes two false dangles.

This tool prints the gate, keeps the suppressed cases in its output, finds the nearest case above
the gate, and exits 3 with no findings when the boundary layer is wrong.

The gap check finds holes in the dissolved coverage. A strip that reaches the outer edge of the
coverage is not a hole and is not found. The report says so on every run, so an exit 0 is not
read as proof that no such strip exists.

```
$ python ng911topo.py --self-test
ng911topo self-test: temporary files only, no network
--------------------------------------------------------------------
...
PASS  a value exactly on the threshold is dropped on every gate  <-- pinned defect
...
info: clipped and uncut areas differ by at most 2.27e-12 sq m over 40 overlapping random pairs
...
PASS  without the re-measure an area a hair under the threshold flips the verdict  <-- pinned defect
PASS  an area within the margin of the threshold is measured again on the uncut polygons  <-- pinned defect
PASS  a clip that fails falls back to the uncut polygons instead of crashing  <-- pinned defect
...
PASS  the gate reports the case just above the threshold and the largest it dropped  <-- pinned defect
...
PASS  two ends 0.0002 m apart are one node, not two false dangles  <-- pinned defect
...
PASS  an end that lands on its own line's body is joined, so a lollipop is not fallout against its neighbour  <-- pinned defect
...
PASS  a boundary edge moved 2 m across an end with a street 6 m away gives fallout on both sides  <-- pinned defect
...
PASS  a boundary layer in the wrong place or CRS is refused with both reasons  <-- pinned defect
...
PASS  a geographic layer that still reads as degrees is caught as re-tagged  <-- pinned defect
...
PASS  the key column gets one apostrophe before a tab and nothing else changes  <-- pinned defect
...
PASS  negative value, x and y stay plain numbers with no apostrophe  <-- pinned defect
...
PASS  a real run over keys that start with = + - @ tab and CR writes each with one apostrophe  <-- pinned defect
...
PASS  a prefix of the write flag is refused and nothing is written  <-- pinned defect
PASS  the prefix --ap is refused by the parser  <-- pinned defect
...
PASS  degree polygons are refused under Web Mercator and nothing is printed  <-- pinned defect
PASS  polygons tagged 3857 under work CRS 3857 are refused and nothing is printed, where a 0.76 sq m overlap read 1.0 and failed the gate  <-- pinned defect
...
PASS  a boundary layer that cannot be read exits 2 and prints no findings, where the unguarded run printed them  <-- pinned defect
PASS  a boundary layer in the wrong place is refused with no findings printed  <-- pinned defect
...
PASS  --overlap-tol nan exits 64 and prints no report, not a clean 0 with a real overlap hidden  <-- pinned defect
...
PASS  a full run opens no socket, and the guard would have caught one
PASS  an unexpected exception exits 70, not the 1 that means a gate failed  <-- pinned defect
...
--------------------------------------------------------------------
287 assertions, 0 failed
```

The full run prints all 287 assertions. Each `...` line above is where this block is cut. The line
that starts with `info:` is a measurement, not an assertion. It is explained in
[The equivalence argument](#the-equivalence-argument-the-clip-and-the-re-measure).

## What already exists

The [QGIS Topology Checker](https://docs.qgis.org/latest/en/docs/user_manual/plugins/core_plugins/plugins_topology_checker.html)
has the rules "must not overlap", "must not have gaps" and "must not have dangles". It lists the
errors in a table, draws them on the map canvas, and zooms to the one you click, so the analyst can
fix it with the digitizing tools. ArcGIS Pro's geodatabase topology has a "Must not overlap" rule,
shows overlaps in red during validation, and treats vertices within a cluster tolerance as one
location. The default cluster tolerance is 10 times the x,y resolution
([Topology in ArcGIS](https://doc.esri.com/en/arcgis-pro/latest/help/data/topologies/topology-in-arcgis.html)).
Both tools are good at this. They show the error on the map, next to the data, and they sit inside
a GUI project. This tool does none of that. Esri's
[Validate Topology](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/validate-topology.html)
tool can also run from `arcpy` in a script, but it validates a geodatabase topology and needs an
ArcGIS licence. This tool reads GeoJSON and needs neither.

What this tool adds is smaller. It is one script that runs in a scheduled job and exits non-zero
when a gate fails. It prints each threshold with the count it suppressed and the nearest case above
it. It writes the suppressed cases to its output file. It refuses a boundary layer that does not
fit the streets. It proves that a reprojection happened, instead of trusting an EPSG tag.

A search of GitHub (`gh search repos`) on the day this was written found QGIS plugins, such as
`wonder-sk/topol` ("Topology checker - plugin for Quantum GIS") and
`Aniketkishorchauhan/Topology-Checker-Pro`, and some topology checkers that have nothing to do with
maps. It found no command-line gate for response areas that keeps its suppressed cases. That shows
only that the search found nothing.

## Requirements

Python 3.9 or newer, [shapely](https://shapely.readthedocs.io/) 2 or newer, and
[pyproj](https://pyproj4.github.io/pyproj/). Neither is in the standard library. No `arcpy`, no
network, no database. The inputs are GeoJSON files, which
every GIS can export.

The self-test prints 287 assertions on every interpreter below, and each run ends with `287
assertions, 0 failed`. `coverage run --branch ng911topo.py --self-test` reports 100 percent of
1377 statements and 284 branches (run on the geo venv row). The count is the same on Windows and
Linux because the self-test needs shapely and pyproj on both and uses no `arcpy`.

| Host | Python | shapely | pyproj |
|---|---|---|---|
| Windows, system install | 3.13.2 | 2.1.2 | 3.7.2 |
| Windows, venv | 3.13.2 | 2.1.2 | 3.8.0 |
| Windows, venv | 3.9.25 | 2.0.7 | 3.6.1 |
| Ubuntu, venv | 3.12.3 | 2.1.2 | 3.8.0 |

The system `python3` on a stock Ubuntu has neither shapely nor pyproj. There `python3
ng911topo.py --self-test` exits 69 and says what is missing. Install both in a virtual environment
first.

The demo runs below produce the same text on the Windows venv and on Ubuntu, with two differences.
Windows turns each line break of the report into CRLF when the output is redirected to a file. The
reprojection line of the EPSG:4326 streets example (`scale ratio` and `PROJ accuracy`) also differs
between the two hosts with the same pyproj 3.8.0 and shapely 2.1.2: Windows prints `0.99973` and
`-1.0`, and Ubuntu prints `0.99972` and `4.0`. The block below shows the Windows values. The
cause was not traced. Neither value changes a verdict. The findings CSV ends every row with CRLF on every host, because `csv.writer` uses that
terminator by default.

```
git clone https://github.com/uhsear/ng911topo.git
```

## Usage

Export each layer to GeoJSON. Name the EPSG code of every file, and name a projected metre CRS to
measure in. The tool never assumes a coordinate system.

```
python ng911topo.py --self-test
python ng911topo.py --polygons areas.geojson --polygons-epsg 4326 --work-epsg 26917
python ng911topo.py --streets roads.geojson --streets-epsg 2237 --work-epsg 26917
python ng911topo.py --streets roads.geojson --streets-epsg 2237 --boundary county.geojson --boundary-epsg 4326 --work-epsg 26917
python ng911topo.py --polygons areas.geojson --polygons-epsg 4326 --work-epsg 26917 --out findings.csv --apply
```

| Flag | Default | What it does |
|---|---|---|
| `--polygons` | none | Response-area polygons. Needs `--polygons-epsg`. Runs the overlap and gap checks. |
| `--streets` | none | Street centerlines. Needs `--streets-epsg`. Runs the snapgap check. |
| `--boundary` | none | Polygons outside which street ends are expected. Needs `--streets` and `--boundary-epsg`. |
| `--polygons-epsg`, `--streets-epsg`, `--boundary-epsg` | none | The EPSG code of each file. Required with the file. |
| `--work-epsg` | none | A projected CRS in metres. Every layer is brought to it. Required. |
| `--key-field` | `KEY` | The polygon property that names an area. A missing key becomes `#` and the feature position. |
| `--street-key-field` | `SEGMENT_ID` | The street property that names a segment. |
| `--overlap-tol` | `1` | Square metres an overlap must exceed. |
| `--gap-tol` | `1` | Square metres an uncovered hole must exceed. |
| `--min-gap-width` | `0.15` | Metres of mean width a hole must exceed. |
| `--snap-tol` | `9` | Metres within which a street end should have snapped. About 30 feet. |
| `--isolated-tol` | the snap tolerance | Metres searched before a dead end is called isolated. |
| `--node-tol` | `0.05` | Metres within which two street ends are one node. Must be under `--snap-tol`. |
| `--bin` | `2` | Width in metres of one histogram bin. |
| `--min-boundary-cover` | `0.5` | Share of street ends that must lie inside `--boundary`. |
| `--allow-enclave` | off | List a hole drawn into one polygon as information. By default it fails the gate. |
| `--sample` | `10` | Findings listed under each heading. `0` lists none. |
| `--out` | none | Path of the findings CSV. Needs `--apply` to be written. |
| `--apply` | off | Write `--out`. Without it nothing is written. |
| `--self-test` | off | Run the assertions and exit. |

Every flag must be spelled in full. The parser sets `allow_abbrev=False`. Python's `argparse`
reads a unique prefix of a long option as that option unless this is set
([argparse](https://docs.python.org/3/library/argparse.html)). Here `--appl` is refused with exit 64,
and so are `--ap`, `--app` and `--apply-`. Nothing is written. The self-test runs all four.

```
$ python ng911topo.py --polygons areas.geojson --polygons-epsg 26917 --work-epsg 26917 --out findings.csv --appl
usage: ng911topo.py [-h] [--polygons POLYGONS] [--polygons-epsg POLYGONS_EPSG]
    ...
ng911topo.py: error: unrecognized arguments: --appl
exit=64
```

The `usage:` text is cut in this block to its first line. The tool prints the whole of it.

`--out` must not be an input layer. That is refused with exit 64, on a dry run too. A file that
already exists is overwritten under `--apply`, with no prompt and no backup. The tool compares
`--out` with the inputs by resolved path only, so a hard link or a Windows short-name alias of an
input is not noticed. Do not give `--out` a hard link to an input.

## The demo files

Every output block below comes from these synthetic layers. They are not shipped as files. Save the
script as `mkdemo.py`, run it in an empty folder, and the layers appear next to it. Coordinates
are in EPSG:26917. The three other street layers are the same streets in US survey feet (EPSG:2237),
in degrees (EPSG:4326) and in Web Mercator (EPSG:3857).

```python
import json
import pyproj
import shapely
from shapely.geometry import Polygon, box, mapping

BX, BY = 400000.0, 3250000.0


def save(name, features):
    with open(name, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": features}, fh)


def area(key, geom):
    geom = shapely.affinity.translate(geom, BX, BY)
    return {"type": "Feature", "properties": {"KEY": key}, "geometry": mapping(geom)}


def street(key, pts):
    coords = [[BX + x, BY + y] for x, y in pts]
    return {"type": "Feature", "properties": {"SEGMENT_ID": key},
            "geometry": {"type": "LineString", "coordinates": coords}}


def frame(tag, x, y, w, h):
    return [(tag + "-W", box(x - 10, y - 10, x, y + h + 10)),
            (tag + "-E", box(x + w, y - 10, x + w + 10, y + h + 10)),
            (tag + "-S", box(x, y - 10, x + w, y)),
            (tag + "-N", box(x, y + h, x + w, y + h + 10))]


rows = [("ZONE-1", box(0, 0, 100, 100)), ("ZONE-2", box(90, 0, 190, 100)),
        ("ZONE-3", box(190, 0, 290, 100)), ("ZONE-4", box(300, 0, 400, 100)),
        ("ZONE-5", box(399.995, 0, 500, 100))]
rows += frame("GAP", 1000, 0, 2, 100) + frame("WIDE", 2000, 0, 10, 100)
rows += frame("CRACK", 3000, 0, 0.1, 100)
ring = Polygon(box(4000, 0, 4040, 40).exterior.coords,
               [box(4015, 15, 4025, 25).exterior.coords])
save("areas.geojson", [area(k, g) for k, g in rows + [("RING", ring)]])

streets = [street("MAIN", [(0, 0), (1000, 0)])]
for n, d in enumerate((6.5, 7.0, 7.2, 7.6)):
    streets.append(street("STUB-%d" % (n + 1), [(100 * (n + 1), d), (100 * (n + 1), 50)]))
streets += [street("LANE", [(700, 1), (700, 60)]), street("EDGE-W", [(5, -305), (5, -30)]),
            street("OUT", [(900, 600), (900, 1100)])]
save("streets.geojson", streets)
save("extent.geojson", [area("EXTENT", box(-20, -310, 1020, 1000))])
save("elsewhere.geojson", [area("ELSEWHERE", box(50000, 0, 51000, 1000))])
save("hot.geojson", [area("=SUM(1+1)", box(0, 0, 10, 10)), area("-ZONE-2", box(5, 0, 15, 10))])


def reproject(epsg, name):
    tr = pyproj.Transformer.from_crs(26917, epsg, always_xy=True)
    moved = [dict(f, geometry={"type": "LineString", "coordinates": [
        list(tr.transform(x, y)) for x, y in f["geometry"]["coordinates"]]}) for f in streets]
    save(name, moved)


reproject(2237, "streets_ft.geojson")
reproject(4326, "streets_ll.geojson")
reproject(3857, "streets_merc.geojson")
```

## The sliver gate

An overlap or a hole passes a gate only when it exceeds the threshold. A value exactly on the
threshold is dropped, on every gate. This rule is pinned, because a gate that sometimes keeps the
edge case and sometimes drops it gives two answers to one question.

Each gate prints three facts: the threshold, how many cases it kept and dropped with the largest it
dropped, and the nearest case above it. The nearest case above is the one to look at. If it is 1.2
against a threshold of 1, the threshold sits in the middle of real data, and a small change moves
the result.

The gap check runs two gates in turn, area and then mean width. Mean width is `2 x area / perimeter`.
For a long thin strip it is close to the strip's width. The demo layer has 18 synthetic polygons.

```
$ python ng911topo.py --polygons areas.geojson --polygons-epsg 26917 --work-epsg 26917
areas.geojson: EPSG:26917 is the work CRS, not transformed; scale ratio 0.9997 against the geodesic distance

OVERLAPS
  18 polygon(s), 15 intersecting pair(s) compared
  area threshold: a case must exceed 1 sq m
  1 over the threshold, 1 suppressed (largest suppressed 0.5 sq m)
  nearest case above the threshold: 1000 sq m
  0 pair(s) within 1e-06 sq m of the threshold were measured again on the uncut polygons
  REPORTED (1)
    ZONE-1 at (400095.00, 3250050.00): 'ZONE-1' overlaps 'ZONE-2' by 1000.0000 sq m
  SUPPRESSED (1)
    ZONE-4 at (400400.00, 3250050.00): 'ZONE-4' overlaps 'ZONE-5' by 0.5000 sq m

GAPS
  4 uncovered hole(s) in the dissolved coverage
  only enclosed holes are checked; a strip that reaches the outer edge of the coverage is not found, so a clean result does not rule it out
  area threshold: a case must exceed 1 sq m
  4 over the threshold, 0 suppressed
  nearest case above the threshold: 10 sq m
  width threshold: a case must exceed 0.15 m
  3 over the threshold, 1 suppressed (largest suppressed 0.0999 m)
  nearest case above the threshold: 1.961 m
  REPORTED (3)
    WIDE-E at (402005.00, 3250050.00): 1000.0000 sq m, mean width 9.0909 m: wide uncovered area ringed by 4 polygons ('WIDE-E', 'WIDE-N', 'WIDE-S', 'WIDE-W'). Verify whether it is a deliberate enclave.
    GAP-E at (401001.00, 3250050.00): 200.0000 sq m, mean width 1.9608 m: covered by no polygon; bordered by 'GAP-E', 'GAP-N', 'GAP-S', 'GAP-W'
    RING at (404020.00, 3250020.00): 100.0000 sq m, mean width 5.0000 m: a hole drawn into 'RING' itself and bordered by no other polygon; the tool cannot tell a deliberate cutout from a missing zone, so check it
  SUPPRESSED (1)
    - at (403000.05, 3250050.00): 10.0000 sq m hole, mean width 0.0999 m, is not over the 0.15 m width gate
exit=1
```

What each result means:

- `ZONE-1` and `ZONE-2` overlap by 1000 sq m. This is reported and fails the gate.
- `ZONE-4` and `ZONE-5` overlap by 0.5 sq m. The gate dropped it. The report says so, and the
  findings file keeps the row. A suppressed case is never lost.
- `GAP-*` leave a strip 2 m wide between four polygons. It is reported as a gap.
- `WIDE-*` leave a hole of 1000 sq m, ringed by four polygons. The tool cannot tell a hole left by
  mistake from a deliberate one. It reports a suspect enclave and asks a person to check. A hole
  with a mean width over 20 times the width gate (at least 3 m) that several polygons border is
  never passed silently.
- `RING` has a hole drawn into the one polygon. The tool cannot tell a deliberate cutout from a
  missing zone, because both look the same. It is reported and fails the gate. If the cutouts are
  known and deliberate, `--allow-enclave` lists them as information and they pass the gate.
- `CRACK-*` leave a hole 0.1 m wide. The width gate dropped it, and the report names its width.

The overlap gate and the width gate each dropped one case in this run. The area gate of the gap
check dropped none. Every dropped case has a line in the report.

With `--allow-enclave`, `RING` moves from REPORTED to INFO. The overlap and the other gaps still
fail the gate, so this run also exits 1:

```
$ python ng911topo.py --polygons areas.geojson --polygons-epsg 26917 --work-epsg 26917 --allow-enclave --sample 0 | grep -A1 -e REPORTED -e INFO
  REPORTED (1)
    ... 1 more; --out lists them all
--
  REPORTED (2)
    ... 2 more; --out lists them all
  INFO (1)
    ... 1 more; --out lists them all
```

## Street ends that stop short (snapgap)

A street end that no other end shares and that is not on another street's body is a dangle. The
tool sorts every dangle into one of three classes.

| Class | Status | What it means |
|---|---|---|
| `fallout` | REPORTED | The end stops within the snap tolerance of another street and is not joined to it. It fails the gate. |
| `isolated` | INFO | A dead end. The nearest street is beyond the snap tolerance. It is listed and passes. |
| `boundary` | SUPPRESSED | The end lies more than the snap tolerance outside the boundary layer, or within the snap tolerance of its edge on either side with no street within the snap tolerance. An end that is suppressed names a street within the snap tolerance, if there is one. |

The distances of the `fallout` ends go into a histogram, and the busiest bin is marked. Four stubs
that stop 6.5 to 7.6 m short of one street show up as one bar. A tool that prints a flat list
shows them as four lines among hundreds.

Two street ends within `--node-tol` of each other are one node, found by single linkage. The tool
does not round to a grid. Two ends 0.0002 m apart can fall either side of a rounding boundary and
look like two dangles. The self-test pins that case. An end within `--node-tol` of another street's
body is a T junction, and it is not a dangle. So is an end that lands on the body of its own line,
such as a P-shaped cul-de-sac drawn as one line.

```
$ python ng911topo.py --streets streets.geojson --streets-epsg 26917 --work-epsg 26917 --sample 4
streets.geojson: EPSG:26917 is the work CRS, not transformed; scale ratio 0.9997 against the geodesic distance

SNAPGAP
  8 street(s), 16 end(s): 0 shared by another street, 0 joined to a street body, 0 on or beyond the boundary, 16 dangling
  snap tolerance 9 m, searched out to 9 m; node tolerance 0.05 m
  no boundary layer: ends at the edge of the data are not suppressed
  distance from a dangling end to the nearest street:
       0.0 -    2.0 m    1  #
       6.0 -    8.0 m    4  ####  <-- densest
  REPORTED (5)
    LANE at (400700.00, 3250001.00): end stops 1.00 m from 'MAIN' and is not joined to it
    STUB-1 at (400100.00, 3250006.50): end stops 6.50 m from 'MAIN' and is not joined to it
    STUB-2 at (400200.00, 3250007.00): end stops 7.00 m from 'MAIN' and is not joined to it
    STUB-3 at (400300.00, 3250007.20): end stops 7.20 m from 'MAIN' and is not joined to it
    ... 1 more; --out lists them all
  INFO (11)
    MAIN at (400000.00, 3250000.00): isolated dead end, nothing within 9 m
    MAIN at (401000.00, 3250000.00): isolated dead end, nothing within 9 m
    STUB-1 at (400100.00, 3250050.00): isolated dead end, nothing within 9 m
    STUB-2 at (400200.00, 3250050.00): isolated dead end, nothing within 9 m
    ... 7 more; --out lists them all
exit=1
```

Without a boundary layer, the report says so on its own line. With one, the ends that leave the area
move to SUPPRESSED and keep a row. The first row below is an end 5 m inside the edge of the layer.
The second is an end beyond it:

```
$ python ng911topo.py --streets streets.geojson --streets-epsg 26917 --boundary extent.geojson --boundary-epsg 26917 --work-epsg 26917 --sample 4
streets.geojson: EPSG:26917 is the work CRS, not transformed; scale ratio 0.9997 against the geodesic distance
extent.geojson: EPSG:26917 is the work CRS, not transformed; scale ratio 0.9997 against the geodesic distance

SNAPGAP
  8 street(s), 16 end(s): 0 shared by another street, 0 joined to a street body, 2 on or beyond the boundary, 14 dangling
  snap tolerance 9 m, searched out to 9 m; node tolerance 0.05 m
  distance from a dangling end to the nearest street:
       0.0 -    2.0 m    1  #
       6.0 -    8.0 m    4  ####  <-- densest
  REPORTED (5)
    LANE at (400700.00, 3250001.00): end stops 1.00 m from 'MAIN' and is not joined to it
    STUB-1 at (400100.00, 3250006.50): end stops 6.50 m from 'MAIN' and is not joined to it
    STUB-2 at (400200.00, 3250007.00): end stops 7.00 m from 'MAIN' and is not joined to it
    STUB-3 at (400300.00, 3250007.20): end stops 7.20 m from 'MAIN' and is not joined to it
    ... 1 more; --out lists them all
  INFO (9)
    MAIN at (400000.00, 3250000.00): isolated dead end, nothing within 9 m
    MAIN at (401000.00, 3250000.00): isolated dead end, nothing within 9 m
    STUB-1 at (400100.00, 3250050.00): isolated dead end, nothing within 9 m
    STUB-2 at (400200.00, 3250050.00): isolated dead end, nothing within 9 m
    ... 5 more; --out lists them all
  SUPPRESSED (2)
    EDGE-W at (400005.00, 3249695.00): end lies within 9 m of the boundary edge, so it is taken to leave the area
    OUT at (400900.00, 3251100.00): end lies outside the boundary layer, so it leaves the area
exit=1
```

## The boundary layer is refused, not dropped

A boundary layer only suppresses. If it fails to load, or loads in the wrong place, it suppresses
nothing, and the run prints every end at the edge of the data as a finding. This tool treats that as
a reason to stop. It checks the layer against the street ends before it uses it. The layer must have
area, it must overlap the streets, and it must hold at least `--min-boundary-cover` of the street
ends once it is grown by the snap tolerance. A layer that fails is refused with exit 3, and no
finding is printed. A layer that cannot be read exits 2, also with no finding. The demo layer
`elsewhere.geojson` is a box 50 km east of the streets.

```
$ python ng911topo.py --streets streets.geojson --streets-epsg 26917 --boundary elsewhere.geojson --boundary-epsg 26917 --work-epsg 26917
refused: elsewhere.geojson cannot be trusted: the boundary layer does not overlap the street layer at all; one of them is in the wrong CRS or the wrong place; only 0% of street ends lie inside the boundary layer, under the 50% required
exit=3
```

```
$ python ng911topo.py --streets streets.geojson --streets-epsg 26917 --boundary nope.geojson --boundary-epsg 26917 --work-epsg 26917
error: [Errno 2] No such file or directory: 'nope.geojson'
exit=2
```

The self-test also runs a flood of twenty short street segments at the edge of the data. With no
boundary layer the run prints them as dangling ends. With a boundary file that is missing, in the
wrong place, or too small, the run prints nothing to standard output.

## The CRS is proved, not trusted

An EPSG tag is a claim. The tool checks five things before it measures anything.

1. The work CRS must be projected and measure in metres. A geographic CRS has no length in its
   units, and a CRS in feet would make every threshold wrong by a factor of 3.28. Either is refused.
2. The data must fall inside the area of use that PROJ lists for the CRS it was tagged with, with a
   0.1 degree margin. Coordinates in metres tagged as feet fall far outside it. PROJ lists
   EPSG:4269 with a west bound (167.65) greater than its east bound (-40.73), because the area
   crosses the antimeridian. The tool reads that as two spans. The self-test pins that case.
3. The data must also fall inside the area of use of the work CRS, with the same margin. A UTM
   zone 1000 km from the data would measure every area wrong, so it is refused.
4. After the transform, the tool compares one pair of vertices before and after, the first and the
   one farthest from it. A projected source must scale by the unit of its input, within 0.5
   percent. A geographic source must no longer look like longitude and latitude, and the output
   distance must match the geodesic distance on the WGS 84 ellipsoid, within 0.5 percent. A layer
   that was re-tagged and not reprojected fails this check. So does a work CRS that is not
   metre-scale at the data, such as Web Mercator.
5. A layer that is already in the work CRS is not transformed. Its scale is still checked: the
   distance between the same two vertices must match the geodesic distance within 0.5 percent. A
   layer tagged Web Mercator under a Web Mercator work CRS is refused, because it would measure
   every area about 1.3 times too large at this latitude.

The area of use comes from [`CRS.area_of_use`](https://pyproj4.github.io/pyproj/stable/api/crs/crs.html).
The geodesic distance comes from [`Geod.inv`](https://pyproj4.github.io/pyproj/stable/api/geod.html)
on the WGS 84 ellipsoid, with longitude first. EPSG:4269 uses the GRS 80 ellipsoid, which differs
from WGS 84 by far less than the 0.5 percent tolerance.

A GeoJSON file that carries a legacy `crs` member must name the same EPSG as the flag, or the run is
refused. RFC 7946 removed that member ([section 4](https://www.rfc-editor.org/rfc/rfc7946#section-4)),
but older files still carry it.

The same streets, given in US survey feet, give the same findings. The note line shows the scale
proof:

```
$ python ng911topo.py --streets streets_ft.geojson --streets-epsg 2237 --work-epsg 26917 --sample 1
streets_ft.geojson: EPSG:2237 -> EPSG:26917, scale ratio 0.99978 against the unit of the input; PROJ accuracy 0.0 (-1 means unknown)

SNAPGAP
  8 street(s), 16 end(s): 0 shared by another street, 0 joined to a street body, 0 on or beyond the boundary, 16 dangling
  snap tolerance 9 m, searched out to 9 m; node tolerance 0.05 m
  no boundary layer: ends at the edge of the data are not suppressed
  distance from a dangling end to the nearest street:
       0.0 -    2.0 m    1  #
       6.0 -    8.0 m    4  ####  <-- densest
  REPORTED (5)
    LANE at (400700.00, 3250001.00): end stops 1.00 m from 'MAIN' and is not joined to it
    ... 4 more; --out lists them all
  INFO (11)
    MAIN at (400000.00, 3250000.00): isolated dead end, nothing within 9 m
    ... 10 more; --out lists them all
exit=1
```

The same streets in degrees give the same findings. The scale proof compares against the
geodesic distance. This block shows only the note line, and the tool prints the whole report:

```
$ python ng911topo.py --streets streets_ll.geojson --streets-epsg 4326 --work-epsg 26917 --sample 1
streets_ll.geojson: EPSG:4326 -> EPSG:26917, output is no longer degree-like; scale ratio 0.99973 against the geodesic distance; PROJ accuracy -1.0 (-1 means unknown)
...
```

Web Mercator and a UTM zone far from the data are refused, because they would measure the data
wrong. At this latitude Web Mercator reads a distance 15 percent long:

```
$ python ng911topo.py --streets streets_ll.geojson --streets-epsg 4326 --work-epsg 3857
refused: streets_ll.geojson: the scale ratio is 1.15024 against the geodesic distance where a metre CRS at this place says 1; the work CRS is not metre-scale here (Web Mercator, EPSG:3857, is not, and a UTM zone far from the data is not). Name a projected CRS for this place
exit=3
```

```
$ python ng911topo.py --streets streets_ll.geojson --streets-epsg 4326 --work-epsg 26910
refused: streets_ll.geojson: the data spans lon -82.03..-82.02, lat 29.37..29.39, outside the area of use of the work CRS EPSG:26910 (lon -126.00..-119.99, lat 30.54..81.80). Name a work CRS that covers the data.
exit=3
```

A layer that is already in Web Mercator is refused the same way when Web Mercator is also the work
CRS, because the check does not depend on a transform having happened:

```
$ python ng911topo.py --streets streets_merc.geojson --streets-epsg 3857 --work-epsg 3857
refused: streets_merc.geojson: EPSG:3857 is the work CRS, but the scale ratio is 1.15024 against the geodesic distance where a metre CRS at this place says 1; the work CRS is not metre-scale here (Web Mercator, EPSG:3857, is not). Name a projected CRS for this place
exit=3
```

The metre file, tagged as feet, is refused:

```
$ python ng911topo.py --streets streets.geojson --streets-epsg 2237 --work-epsg 26917
refused: streets.geojson: the data spans lon -82.84..-82.83, lat 33.27..33.27, outside the area of use of its CRS (lon -83.34..-81.13, lat 26.27..29.60). The EPSG tag is probably wrong.
exit=3
```

The proof covers units and the scale of the work CRS. It does not prove that the datum shift was right. The accuracy figure in the
note is whatever pyproj reports for the operation
([Transformer.accuracy](https://pyproj4.github.io/pyproj/stable/api/transformer.html) says -1 means
unknown). Here it prints 0.0, and the tool does not interpret it.

## The equivalence argument: the clip and the re-measure

To find the area two polygons share, the tool intersects them. A large polygon makes that slow, so
it first cuts both polygons to the box their bounding boxes share. This is the clip.

On paper the clip changes nothing. Let `R` be the box that both bounding boxes share. Nothing outside
`R` can lie in both polygons, so the shared ground `A and B` is a subset of `R`. The clip gives
`A and R` and `B and R`. Their intersection is `A and B and R`, which is `A and B`. The two ways of
measuring are equal.

On a machine they are not bitwise equal. The clip computes new vertices where the edges of a
polygon cross the sides of `R`, and floating-point arithmetic rounds each one. shapely documents
`clip_by_rect` as fast but possibly dirty. Its output is not guaranteed to be valid, and it raises no
error for topological problems
([clip_by_rect](https://shapely.readthedocs.io/en/stable/reference/shapely.clip_by_rect.html)).

That matters because the gate has a sharp edge. Take two polygons that share 1.0000001 sq m, against
a gate at 1 sq m. The pair is reported. If the clip loses 2.5e-7 sq m, the same pair measures just
under 1 and is suppressed. The argument said the optimisation was exact. A measurement says the
verdict changed.

The tool therefore keeps the clip and adds a re-measure. When a clipped area lies within
`CLIP_EPS` (1e-6 sq m) of the threshold, the tool measures the pair again on the uncut polygons and
uses that area. Away from the threshold the clip cannot change the verdict, as long as its error is
smaller than `CLIP_EPS`. The report prints how many pairs were measured again. If the clip raises a
GEOS error, the tool falls back to the uncut polygons for that pair.

What is measured and what is argued:

- The self-test clips 40 random overlapping pairs of 120-vertex shapes and compares each with the
  uncut area. The largest difference is 2.27e-12 sq m, on every host that ran it. That is a measurement of
  40 pairs. It is not a proof for every shape.
- The margin `CLIP_EPS` is about 440,000 times larger than that. The margin is a choice, not a
  derived bound. A clip that is wrong by more than the margin on a pair far from the threshold would
  not be caught.
- A test replaces the clip with one that loses 2.5e-7 sq m. Without the re-measure the verdict on a
  pair a hair under the threshold flips. With it the verdict does not. Both results are asserted.
- A second test makes the clip raise a GEOS error. The pair is measured on the uncut polygons and
  the run does not crash.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Something was compared and no gate failed. INFO and SUPPRESSED findings do not fail a gate. |
| 1 | A gate failed: an overlap, a gap or a street end that stops short. A failed `--self-test` also returns 1, so read its `N assertions, M failed` footer, not only the code. |
| 2 | A file could not be read or written. The message names the file. Any `ValueError` raised inside the analysis, including one from a library, also exits 2. |
| 3 | The run was refused: a CRS, a geometry or the boundary layer cannot be trusted. Nothing goes to standard output. |
| 4 | Nothing was compared, or a named layer held no features. When one layer was empty and the other was compared, the report of the other layer prints first, and the exit is 4 even if that layer failed a gate. Neither case is reported as clean. |
| 64 | Usage error, including an abbreviated flag and a threshold that is `nan` or `inf`. |
| 69 | shapely 2 or pyproj is not installed. |
| 70 | An unexpected crash. This is a fault in the tool and is never a finding. Output sent to a pipe that closes early, such as `head`, ends here with a traceback. |

A polygon that is not valid refuses the run with exit 3, because every area derived from it would be
wrong. A feature with no geometry is counted in the notes and skipped.

## The findings file

`--out` with `--apply` writes every finding, reported or not, so a suppression can be audited later.
Without `--apply` the tool prints `Check only` and writes nothing.

```
$ python ng911topo.py --polygons areas.geojson --polygons-epsg 26917 --work-epsg 26917 --out findings.csv --sample 0
areas.geojson: EPSG:26917 is the work CRS, not transformed; scale ratio 0.9997 against the geodesic distance

OVERLAPS
  18 polygon(s), 15 intersecting pair(s) compared
  area threshold: a case must exceed 1 sq m
  1 over the threshold, 1 suppressed (largest suppressed 0.5 sq m)
  nearest case above the threshold: 1000 sq m
  0 pair(s) within 1e-06 sq m of the threshold were measured again on the uncut polygons
  REPORTED (1)
    ... 1 more; --out lists them all
  SUPPRESSED (1)
    ... 1 more; --out lists them all

GAPS
  4 uncovered hole(s) in the dissolved coverage
  only enclosed holes are checked; a strip that reaches the outer edge of the coverage is not found, so a clean result does not rule it out
  area threshold: a case must exceed 1 sq m
  4 over the threshold, 0 suppressed
  nearest case above the threshold: 10 sq m
  width threshold: a case must exceed 0.15 m
  3 over the threshold, 1 suppressed (largest suppressed 0.0999 m)
  nearest case above the threshold: 1.961 m
  REPORTED (3)
    ... 3 more; --out lists them all
  SUPPRESSED (1)
    ... 1 more; --out lists them all

Check only. findings.csv was not written. Re-run with --apply.
exit=1
```

```
$ python ng911topo.py --polygons areas.geojson --polygons-epsg 26917 --work-epsg 26917 --out findings.csv --apply --sample 0 | tail -2

wrote findings.csv
```

```
$ cat findings.csv (first 110 columns)
check,status,class,key,other,value,x,y,detail
overlap,reported,overlap,ZONE-1,ZONE-2,1000.0000,400095.000,3250050.000,'ZONE-1' overlaps 'ZONE-2' by 1000.000
overlap,suppressed,overlap,ZONE-4,ZONE-5,0.5000,400399.997,3250050.000,'ZONE-4' overlaps 'ZONE-5' by 0.5000 sq
gap,reported,suspect-enclave,WIDE-E,"'WIDE-E', 'WIDE-N', 'WIDE-S', 'WIDE-W'",1000.0000,402005.000,3250050.000,
gap,reported,gap,GAP-E,"'GAP-E', 'GAP-N', 'GAP-S', 'GAP-W'",200.0000,401001.000,3250050.000,"200.0000 sq m, me
gap,reported,enclave,RING,,100.0000,404020.000,3250020.000,"100.0000 sq m, mean width 5.0000 m: a hole drawn into 
gap,suppressed,narrow,,,10.0000,403000.050,3250050.000,"10.0000 sq m hole, mean width 0.0999 m, is not over th
```

The columns are `check`, `status`, `class`, `key`, `other`, `value`, `x`, `y` and `detail`. The `x`
and `y` are in the work CRS. A row sits inside the overlap or hole, so an analyst can zoom to it.

### Keys that start like a formula

The `key` and `other` columns, and the `detail` text that quotes them, come from the input file. A
spreadsheet runs a cell that starts with `=`, `+`, `-` or `@` as a formula, and OWASP also lists a
tab and a carriage return
([CSV Injection](https://owasp.org/www-community/attacks/CSV_Injection)). The tool puts one
apostrophe in front of such a text cell. It guards the six text columns: `check`, `status`,
`class`, `key`, `other` and `detail`. The `value`, `x` and `y` columns are numbers and are never
changed, so a negative coordinate stays `-10.000`. Only the file is guarded. The terminal report
shows the key as it came. The tool never reads its own CSV back, so nothing strips the prefix.

```
$ python ng911topo.py --polygons hot.geojson --polygons-epsg 26917 --work-epsg 26917 --out hot.csv --apply
$ cat hot.csv
check,status,class,key,other,value,x,y,detail
overlap,reported,overlap,'=SUM(1+1),'-ZONE-2,50.0000,400007.500,3250005.000,'=SUM(1+1)' overlaps '-ZONE-2' by 50.0000 sq m
```

`hot.geojson` comes from the `mkdemo.py` script above. It holds two 10 m squares offset by 5 m,
keyed `=SUM(1+1)` and `-ZONE-2`. Without the guard the file held both keys as written.

## Limits

- It checks street ends against other streets. It does not yet check that a road crosses an
  area boundary without a split. The first plan for this repo listed that check. It is not built.
- GeoJSON only. No geodatabase, no shapefile, no SDE connection. Reading a geodatabase would need
  `arcpy`.
- Both layers are read into memory. In a one-off check on the Windows host, a synthetic grid of 2500
  polygons and a set of 3000 streets each ran in under two seconds. That check is not in the
  self-test. A statewide layer is not what this is for.
- The gap check finds holes in the dissolved coverage. A strip along the outer edge of the whole
  coverage is not a hole and is not found. An open channel that reaches the outer edge is not a hole.
  A strip that runs across the whole coverage, such as a 5 m strip that fully separates two
  polygons, reaches the outer edge at both ends. It is not a hole either, and the run exits 0 with
  `0 uncovered hole(s)`.
  The report prints a line that says so on every run. Treat an exit 0 as "no enclosed hole", not
  as "no uncovered ground".
- The default thresholds are starting values for a metre CRS, not measurements. Measure your own
  layers. The nearest case above each gate is printed so that you can.
- A hole that is drawn into one polygon fails the gate until `--allow-enclave` is given. With the
  flag it is listed as information and the run can exit 0 on a zone that someone forgot to draw. A
  wide hole ringed by several polygons is a suspect, and the tool cannot tell more than that.
- A legacy GeoJSON `crs` member written as an OGC URL, such as
  `http://www.opengis.net/def/crs/EPSG/0/26917`, is refused with exit 3 although it is valid. Only
  the `EPSG:NNNN`, `urn:ogc:def:crs:EPSG::NNNN` and `CRS84` spellings are read. The tool never
  fetches the URL.
- The apostrophe guard was verified by reading the CSV back, not by opening it in a spreadsheet.
  The README does not claim how Excel shows it.
- The reprojection proof covers units. It does not prove the datum shift.
- The tool checks the CRS tag by area of use and by unit scale only. A layer in metres that is
  tagged with a feet CRS passes both checks if its coordinates fall inside the area of use of that
  CRS.
- An axis-swapped GeoJSON file (latitude first) tagged EPSG:4326 passes, because the area of use of
  4326 is the whole world. The proof cannot catch swapped axes on a world-wide CRS, and areas and
  distances then come out wrong.
- A threshold given as `nan` or `inf`, such as `--overlap-tol nan`, is refused with exit 64 before
  any file is read, because every comparison with `nan` is false and the run would drop every case.
  A threshold of `0` is allowed.
- A coordinate of `NaN` or `Infinity` in GeoJSON is not valid JSON, but Python reads it. Either one
  is refused with exit 3 and a message that names the layer, before any distance is measured. A
  `NaN` used to pass the area-of-use check and hide a real stub, so the run exited 0 on a case it
  could not evaluate. GeoJSON from standard tools never carries `NaN`.
- A GeoJSON file saved with a UTF-8 byte order mark exits 2 with `Unexpected UTF-8 BOM (decode
  using utf-8-sig)`. It is refused and never read as clean, but Windows tools write such files
  often. Save the file as UTF-8 without a BOM.
- A GeoJSON file in Latin-1, UTF-16 or another encoding that is not UTF-8 exits 2 with a decode
  error. Only UTF-8 is read.
- The findings CSV is UTF-8 without a byte order mark, so Excel may show a non-ASCII key wrongly.
- A key that holds an escape character or a line break is printed to the terminal as it came, so a
  hostile file can clear the screen, set the window title or forge a line that looks like a
  finding. Run the tool on files you trust.
- A street end more than the snap tolerance outside the boundary layer is SUPPRESSED even when
  another street is nearer than the snap tolerance. The row names that street, but the run can
  exit 0 on a real gap beyond the edge. An end within the snap tolerance of the edge, inside or
  outside, is suppressed only when no street is within the snap tolerance. Otherwise it is fallout
  and fails the gate, so a boundary digitized a metre or two off the true line gives the same
  verdict on both sides.
  A boundary layer that holds only half of the street ends passes the default check and then
  hides the fallout at the other half. Raise `--min-boundary-cover` for a partial layer.
- Street ends within `--node-tol` of each other count as one shared node, so a dangling stub of
  0.05 m or less is never reported. A closed loop whose two ends meet within that distance, and that
  joins no other street, is not reported either. A zero-length street is one node and is never
  reported.
- The wording of some messages, the counts in the histogram header and the order of INFO and
  SUPPRESSED rows are checked by the self-test only for their main phrases, not word for word.
- A stock Linux `python3` may have no shapely. The self-test then exits 69, and it needs a virtual
  environment with shapely 2 and pyproj. Python 3.9 was checked in a virtual environment with
  shapely 2.0.7 and pyproj 3.6.1.
- A MultiLineString becomes one street per part. Z values are dropped.
- The findings file is written in place, not through a temporary file. The text is encoded before
  the file is opened, so a key that UTF-8 cannot hold, such as a lone surrogate (JSON `"\ud800x"`),
  exits 2 and leaves an existing file untouched. A disk error during the write can still leave a
  partial file, and the exit code is 2.
- The formula guard covers the six leaders `=`, `+`, `-`, `@`, tab and carriage return. OWASP also
  lists a line feed and the full-width forms of `=`, `+`, `-` and `@`. A key that starts with one of
  those is written as it came. A key with spaces before a formula leader, such as ` =2+2`, is also
  written as it came. This tool assumes a spreadsheet does not treat it as a formula. That was not tried in a spreadsheet, and the OWASP page does not mention leading spaces. OWASP also notes that its mitigations are not reliable in Excel after the file
  is saved and opened again. The apostrophe shows in a viewer that does not treat it as a text marker,
  for example a text editor or a script that reads the CSV. A key that already starts with an
  apostrophe is not changed, so the guard cannot be undone exactly.
- A layer whose every vertex is at one location, such as a single zero-length street, is refused with
  exit 3, because the scale of the work CRS cannot be proved from one point.
- With shapely 2.2 or newer, the tool prints a `DeprecationWarning` for `shapely.ops.transform` to
  stderr on each run that reprojects a layer. This was seen on Linux with Python 3.12.3 and shapely
  2.2.0. The verdict and the exit code do not change, and the table in Requirements lists shapely only
  up to 2.1.2.
- Do not run the tool with `python -W error`. The self-test then fails one assertion, because a
  `nan` coordinate makes shapely warn and the run exits 2 instead of 3. With shapely 2.2 or newer, a
  run that reprojects a layer exits 70 on the deprecation warning above.
- A GeoJSON file nested about 100000 levels deep hits the recursion limit of Python and exits 70
  with a `RecursionError` traceback.
- A pyproj error other than a CRS error during a transform, such as a `ProjError`, exits 70 as a
  crash, not 3 as a refusal.
- With `--apply`, a run that exits 4 because every layer was empty returns before it writes, so a
  findings file from an earlier run stays in place. A job should read the exit code, not look for
  the file.
- A typo in `--key-field` gives no warning. Every key becomes `#<position>`, and the run still
  exits 0 or 1 on the result.
- A feature with no geometry is counted in a note and skipped. A layer that is half null still
  exits 0 or 1 on its other half.
- The no-socket assertion replaces `socket.connect` in Python, so it cannot see a network fetch that
  the PROJ library makes by itself. That is the `PROJ_NETWORK=ON` case named below.
- A character that the console cannot show prints as `?`. The file is always UTF-8.
- The ArcGIS pages for the "must not have gaps" and "must not have dangles" rules did not load when
  this was written, so the comparison above cites only the QGIS manual for those two rules.
- Network: none. PROJ reads `PROJ_NETWORK` from the environment, and the tool never sets it
  ([PROJ environment variables](https://proj.org/en/stable/usage/environmentvars.html)). With
  `PROJ_NETWORK=ON`, PROJ may fetch a datum grid during a reprojection, and then this line no longer
  holds. Apart from that, the tool opens no socket. The self-test replaces `socket.connect`, runs the
  tool, and asserts that no connection was tried, and that its own guard would have caught one. There is
  no stub server because there is nothing to call.
- Every line and branch of the module is reached by the self-test, so there is no uncovered line to
  explain.
- There is no `--fix`. The tool names what it finds. Deciding is an edit somebody signs for.
- `--self-test` ignores every other valid flag. It writes only to a temporary directory, which it
  removes at the end, even when an assertion raises.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

- [sidestamp](https://github.com/uhsear/sidestamp) checks the values stamped on each side of a
  centerline against the polygon that side fronts.
- [ringwind](https://github.com/uhsear/ringwind) names GeoJSON rings wound the wrong way. Run it
  first if a polygon is refused as not valid.
