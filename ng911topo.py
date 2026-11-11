#!/usr/bin/env python
"""Find response-area polygons that overlap, enclosed ground no polygon covers, and street ends that stop short.

Reports its own thresholds, what each threshold suppressed, and the nearest
case just above it. Refuses to run when a boundary layer it was told to use
cannot be trusted, instead of printing a flood of false findings.

Response-area layers carry no error when two areas overlap by a sliver or
leave a strip that belongs to neither. A 911 call from inside that strip
routes to the wrong agency, or to no agency. A width filter that drops
digitizing noise can also drop a real gap, if nobody looks at what it removed.
The same fate hits a street layer: a list of dangling ends hides the ones that
stop 7 metres short of another road among hundreds that are real dead ends.

The tools that already do this are the QGIS Topology Checker plugin and the
geodatabase topology rules in ArcGIS Pro. Both are good at it, both draw the
errors on a map, and both work inside a GUI project. This one is a single
script that runs in a scheduled job, gates on an exit code, and keeps the
suppressed cases in its output.

    python ng911topo.py --self-test
    python ng911topo.py --polygons areas.geojson --polygons-epsg 4326 --work-epsg 26917
    python ng911topo.py --streets roads.geojson --streets-epsg 2237 --work-epsg 26917
    python ng911topo.py --streets roads.geojson --streets-epsg 2237 \\
        --boundary county.geojson --boundary-epsg 4326 --work-epsg 26917
    python ng911topo.py --polygons areas.geojson --polygons-epsg 4326 \\
        --work-epsg 26917 --out findings.csv --apply

Every input names its EPSG code. The tool never assumes one. Inputs are
reprojected to the work CRS, which must be projected and in metres, and the
reprojection is checked: the data must fall inside the area of use of the
stated CRS and of the work CRS, and the scale of the output must match the
unit of the input, or the geodesic distance for a geographic input.
Nothing is written without --apply.

Exit codes: 0 something was compared and no gate failed, 1 a gate failed
(an overlap, a gap or a street end that stops short), 2 a file could not be
read or written, 3 the run was refused (a CRS, a geometry or the boundary
layer cannot be trusted), 4 nothing was compared or a named layer held no
features, 64 usage error, 69 shapely
or pyproj is missing, 70 an unexpected crash, which is never a finding.
"""

from __future__ import print_function

import argparse
import contextlib
import csv
import importlib
import importlib.util
import io
import json
import math
import os
import random
import re
import shutil
import socket
import sys
import tempfile
import traceback

# =============================================================================
# CONFIGURATION. Deliberately not flags unless a flag exists. These are
# starting values for a metre CRS, not measurements from any shipped dataset.
# Measure your own layers before trusting them.
# =============================================================================

# Overlap area, in square metres, at or under which two polygons are not
# reported as overlapping. A shared edge digitized twice leaves slivers.
DEFAULT_OVERLAP_TOL = 1.0

# Area, in square metres, at or under which an uncovered hole is dropped.
DEFAULT_GAP_TOL = 1.0

# Mean width (2 x area / perimeter), in metres, at or under which an uncovered
# hole is dropped as a crack along a shared edge. 0.15 m is about half a foot.
DEFAULT_MIN_GAP_WIDTH = 0.15

# A hole this many times wider than the width gate, ringed by more than one
# polygon, is called a suspect enclave. Only a hole drawn into one polygon is
# provably deliberate, and a pocket two zones border looks the same by shape.
ENCLAVE_SUSPECT_MULT = 20.0

# How far a polygon may sit from a hole and still count as bordering it.
BORDER_PAD = 0.01

# A street end this close (metres) to another street is a dangle that should
# have snapped. Roughly 30 feet.
DEFAULT_SNAP_TOL = 9.0

# Two street ends this close (metres) are one node. Clustering by distance
# replaces rounding to a grid, where two ends 0.0002 m apart can fall either
# side of a rounding boundary and look like two dangles.
DEFAULT_NODE_TOL = 0.05

# Width of one histogram bin (metres) over the distance from a dangling end to
# the nearest street.
DEFAULT_BIN = 2.0

# A histogram bin holding at least this many ends is marked as the densest.
MIN_CLUSTER = 3

# The share of street ends that must lie inside the boundary layer, grown by
# the snap tolerance, before the boundary layer is trusted.
DEFAULT_MIN_BOUNDARY_COVER = 0.5

# An overlap area within this many square metres of the threshold is measured
# again against the uncut polygons. See the README section on the clip.
CLIP_EPS = 1e-6

# The length of a transformed segment, divided by the length it should have
# from the unit of the input, may differ from 1 by this much.
PROOF_TOL = 0.005

# Input data may fall this many degrees outside the area of use of its CRS.
AOU_MARGIN_DEG = 0.1

# How many findings are listed under each heading.
DEFAULT_SAMPLE = 10

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

EXIT_OK = 0
EXIT_GATE = 1
EXIT_IO = 2
EXIT_REFUSED = 3
EXIT_NOTHING = 4
EXIT_USAGE = 64
EXIT_LIBS = 69
EXIT_CRASH = 70

# Finding status. Only REPORTED fails the gate. INFO is listed and passes.
# SUPPRESSED is a case a gate or the boundary layer removed, kept for audit.
REPORTED = "reported"
INFO = "info"
SUPPRESSED = "suppressed"

OUT_COLUMNS = ("check", "status", "class", "key", "other", "value", "x", "y",
               "detail")

POLYGONS = ("Polygon", "MultiPolygon")

shapely = None
pyproj = None


class Refusal(Exception):
    """The run cannot be trusted. Exit 3, with nothing reported as a finding."""


class MissingLibs(Exception):
    """shapely 2 or pyproj is not installed. Exit 69."""


class Feat(object):
    """One geometry and the key the operator knows it by."""

    def __init__(self, index, key, geom):
        self.index = index
        self.key = key
        self.geom = geom


# A spreadsheet runs a cell that starts with one of these as a formula. Text
# from the input files reaches the CSV as key, other and detail.
FORMULA_LEADERS = ("=", "+", "-", "@", chr(9), chr(13))


def cell(value):
    """A CSV text cell that a spreadsheet will not run as a formula: one
    apostrophe goes in front of a leading = + - @ tab or CR. The numeric
    columns (value, x, y) never pass through here, so they stay numbers.
    The tool never reads its own CSV back, so nothing strips the prefix."""
    if value[:1] in FORMULA_LEADERS:
        return "'" + value
    return value


class Finding(object):
    """One row of output: a reported case, an informational one, or a
    suppressed one kept so the suppression can be audited."""

    def __init__(self, check, status, cls, key, other, value, x, y, detail):
        self.check = check
        self.status = status
        self.cls = cls
        self.key = key
        self.other = other
        self.value = value
        self.x = x
        self.y = y
        self.detail = detail

    def row(self):
        return [cell(self.check), cell(self.status), cell(self.cls),
                cell(self.key), cell(self.other),
                "" if self.value is None else "%.4f" % self.value,
                "%.3f" % self.x, "%.3f" % self.y, cell(self.detail)]


class Gate(object):
    """What a threshold did: what passed it, what it removed, and the two
    cases either side of it."""

    def __init__(self, threshold, kept, dropped):
        self.threshold = threshold
        self.kept = kept
        self.dropped = dropped
        self.nearest_above = min([v for v, _ in kept]) if kept else None
        self.largest_dropped = max([v for v, _ in dropped]) if dropped else None


def gate(pairs, threshold):
    """Split (value, item) pairs at threshold. A value must exceed it to be
    kept, so a value exactly on the threshold is dropped, on every gate."""
    kept = [p for p in pairs if p[0] > threshold]
    dropped = [p for p in pairs if not p[0] > threshold]
    return Gate(threshold, kept, dropped)


def gate_text(label, g, unit):
    """The three sentences a gate owes the reader."""
    lines = ["  %s threshold: a case must exceed %.4g %s"
             % (label, g.threshold, unit)]
    seen = "%d over the threshold, %d suppressed" % (len(g.kept), len(g.dropped))
    if g.largest_dropped is not None:
        seen += " (largest suppressed %.4g %s)" % (g.largest_dropped, unit)
    lines.append("  " + seen)
    if g.nearest_above is None:
        lines.append("  no case above the threshold")
    else:
        lines.append("  nearest case above the threshold: %.4g %s"
                     % (g.nearest_above, unit))
    return lines


# ------------------------------------------------------------------ libraries

def load_libs(importer=importlib.import_module):
    """Import shapely 2 and pyproj. The globals change only on success."""
    global shapely, pyproj
    try:
        sh = importer("shapely")
        importer("shapely.geometry")
        importer("shapely.ops")
        importer("shapely.errors")
        pp = importer("pyproj")
    except ImportError as exc:
        raise MissingLibs("ng911topo needs shapely 2 and pyproj: %s" % exc)
    major = int((re.findall(r"\d+", sh.__version__) or ["0"])[0])
    if major < 2:
        raise MissingLibs("ng911topo needs shapely 2 or later, found %s"
                          % sh.__version__)
    shapely, pyproj = sh, pp


# ------------------------------------------------------------------------ CRS

def crs_facts(epsg):
    """What PROJ says about an EPSG code, reduced to the facts this tool uses."""
    try:
        crs = pyproj.CRS.from_epsg(epsg)
    except pyproj.exceptions.CRSError:
        raise Refusal("EPSG:%d is not a code PROJ knows" % epsg)
    axes = crs.axis_info
    aou = crs.area_of_use
    return {"epsg": epsg,
            "geographic": bool(crs.is_geographic),
            "projected": bool(crs.is_projected),
            "factors": tuple(float(a.unit_conversion_factor)
                             for a in axes[:2]),
            "unit": axes[0].unit_name,
            "aou": tuple(aou.bounds) if aou is not None else None}


def work_crs_problem(facts):
    """Why a CRS cannot be the work CRS, or None. Distances are only
    meaningful in a projected CRS whose axes are both metres."""
    if not facts["projected"]:
        return ("EPSG:%d is not a projected CRS, so its distances are not "
                "lengths" % facts["epsg"])
    if facts["factors"] != (1.0, 1.0):
        return ("EPSG:%d measures in %s, and this tool's thresholds are in "
                "metres" % (facts["epsg"], facts["unit"]))
    return None


def aou_verdict(aou, bounds, margin, whose="its CRS",
                advice="The EPSG tag is probably wrong."):
    """Whether data at bounds (west, south, east, north in degrees) lies in
    the area of use of a CRS, the one it is tagged with or the work CRS.
    Returns (problem, note). A comparison that fails on nan refuses, which is
    the safe side."""
    if aou is None:
        return None, "area of use unknown to PROJ, so the tag is unchecked"
    w, s, e, n = aou
    x0, y0, x1, y1 = bounds
    if w <= e:
        lon_ok = w - margin <= x0 and x1 <= e + margin
    else:
        # PROJ lists west > east for an area that crosses the antimeridian,
        # as EPSG:4269 does (lon 167.65 to -40.73).
        lon_ok = all(x >= w - margin or x <= e + margin for x in (x0, x1))
    if lon_ok and s - margin <= y0 and y1 <= n + margin:
        return None, None
    return ("the data spans lon %.2f..%.2f, lat %.2f..%.2f, outside the area "
            "of use of %s (lon %.2f..%.2f, lat %.2f..%.2f). %s"
            % (x0, x1, y0, y1, whose, w, e, s, n, advice)), None


def prove_reprojection(src_geographic, factor, a_src, b_src, a_out, b_out,
                       geodesic=None):
    """Decide from two vertices, before and after, whether the layer was
    reprojected and not merely re-tagged. Returns (ok, text).

    A projected source is checked by scale: the output distance between the
    two vertices must equal the input distance times the metres-per-unit of
    the input CRS. A geographic source has no length in degrees to compare,
    so the output must differ from the input and must no longer fit in the
    range of longitude and latitude. Then, when the caller passes the geodesic
    distance in metres between the two vertices, the output distance must
    match it. That catches a work CRS that is far from the data or is not
    metre-scale there (Web Mercator). This proves units and the scale of the
    work CRS, not the datum shift."""
    if src_geographic:
        moved = tuple(a_out) != tuple(a_src)
        big = max(abs(a_out[0]), abs(a_out[1])) > 360.0
        if not (moved and big):
            return False, ("the output still looks like longitude and "
                           "latitude, so the layer was re-tagged, not "
                           "reprojected")
        if geodesic is None:
            return True, "output is no longer degree-like"
        if geodesic <= 0.0:
            return False, ("every vertex is at one location, so the scale "
                           "of the reprojection cannot be proved")
        ratio = math.hypot(b_out[0] - a_out[0], b_out[1] - a_out[1]) / geodesic
        if abs(ratio - 1.0) <= PROOF_TOL:
            return True, ("output is no longer degree-like; scale ratio "
                          "%.5f against the geodesic distance" % ratio)
        return False, ("the scale ratio is %.5f against the geodesic "
                       "distance where a metre CRS at this place says 1; "
                       "the work CRS is not metre-scale here (Web Mercator, "
                       "EPSG:3857, is not, and a UTM zone far from the data "
                       "is not). Name a projected CRS for this place"
                       % ratio)
    d_src = math.hypot(b_src[0] - a_src[0], b_src[1] - a_src[1])
    if d_src == 0.0:
        return False, ("every vertex is at one location, so the scale of the "
                       "reprojection cannot be proved")
    ratio = math.hypot(b_out[0] - a_out[0], b_out[1] - a_out[1]) / (d_src * factor)
    if abs(ratio - 1.0) <= PROOF_TOL:
        return True, "scale ratio %.5f against the unit of the input" % ratio
    return False, ("the scale ratio is %.5f where the unit of the input says "
                   "1; the layer was re-tagged, not reprojected, or its CRS "
                   "is not metre-scale at this latitude (Web Mercator, "
                   "EPSG:3857, is not). Reproject it to a local projected "
                   "CRS first" % ratio)


def prove_work_scale(planar, geodesic):
    """Decide whether a layer already in the work CRS measures in true metres
    at its place. planar is the distance in work units between the two most
    distant sampled vertices, geodesic the distance in metres between the
    same two. Returns (ok, text). A CRS that says metres can still be far
    from metre-scale at the data, as Web Mercator (EPSG:3857) is."""
    if planar <= 0.0 or geodesic <= 0.0:
        return False, ("every vertex is at one location, so the scale of the "
                       "work CRS cannot be proved")
    ratio = planar / geodesic
    if abs(ratio - 1.0) <= PROOF_TOL:
        return True, ("scale ratio %.4f against the geodesic distance"
                      % ratio)
    return False, ("the scale ratio is %.5f against the geodesic distance "
                   "where a metre CRS at this place says 1; the work CRS is "
                   "not metre-scale here (Web Mercator, EPSG:3857, is not). "
                   "Name a projected CRS for this place" % ratio)


def convert_layer(feats, src, work, label, margin=AOU_MARGIN_DEG):
    """Reproject feats from src to work and prove it happened. Returns
    (feats, note). Raises Refusal when the tag or the transform is wrong."""
    geoms = [f.geom for f in feats]
    if not all(math.isfinite(v) for xy in shapely.get_coordinates(geoms).tolist()
               for v in xy):
        raise Refusal("%s: a coordinate is nan or infinite, so no distance "
                      "from it can be trusted; repair the file" % label)
    xmin, ymin, xmax, ymax = [float(v) for v in shapely.total_bounds(geoms)]
    to_ll = pyproj.Transformer.from_crs(src["epsg"], 4326, always_xy=True)
    lons, lats = to_ll.transform([xmin, xmin, xmax, xmax],
                                 [ymin, ymax, ymin, ymax])
    bounds = (min(lons), min(lats), max(lons), max(lats))
    problem, aou_note = aou_verdict(src["aou"], bounds, margin)
    if problem:
        raise Refusal("%s: %s" % (label, problem))
    before = shapely.get_coordinates(geoms).tolist()
    far = max(range(len(before)),
              key=lambda n: (before[n][0] - before[0][0]) ** 2
              + (before[n][1] - before[0][1]) ** 2)
    if src["epsg"] == work["epsg"]:
        lons, lats = to_ll.transform([before[0][0], before[far][0]],
                                     [before[0][1], before[far][1]])
        ok, text = prove_work_scale(
            math.hypot(before[far][0] - before[0][0],
                       before[far][1] - before[0][1]),
            pyproj.Geod(ellps="WGS84").inv(lons[0], lats[0], lons[1],
                                           lats[1])[2])
        if not ok:
            raise Refusal("%s: EPSG:%d is the work CRS, but %s"
                          % (label, src["epsg"], text))
        note = ("%s: EPSG:%d is the work CRS, not transformed; %s"
                % (label, src["epsg"], text))
        return feats, note + ("; " + aou_note if aou_note else "")
    problem, _ = aou_verdict(
        work["aou"], bounds, margin, "the work CRS EPSG:%d" % work["epsg"],
        "Name a work CRS that covers the data.")
    if problem:
        raise Refusal("%s: %s" % (label, problem))
    tr = pyproj.Transformer.from_crs(src["epsg"], work["epsg"], always_xy=True)
    out = [shapely.ops.transform(tr.transform, g) for g in geoms]
    after = shapely.get_coordinates(out).tolist()
    geodesic = None
    if src["geographic"]:
        geodesic = pyproj.Geod(ellps="WGS84").inv(
            before[0][0], before[0][1], before[far][0], before[far][1])[2]
    ok, text = prove_reprojection(src["geographic"], src["factors"][0],
                                  before[0], before[far], after[0], after[far],
                                  geodesic)
    if not ok:
        raise Refusal("%s: %s" % (label, text))
    note = ("%s: EPSG:%d -> EPSG:%d, %s; PROJ accuracy %s (-1 means unknown)"
            % (label, src["epsg"], work["epsg"], text, tr.accuracy))
    if aou_note:
        note += "; " + aou_note
    return [Feat(f.index, f.key, g) for f, g in zip(feats, out)], note


# ------------------------------------------------------------------- reading

def declared_epsg(doc):
    """The EPSG code a legacy GeoJSON crs member declares, or None when the
    file declares nothing. RFC 7946 removed the member; older files carry it."""
    crs = doc.get("crs")
    if crs is None:
        return None
    props = crs.get("properties") if isinstance(crs, dict) else None
    name = str(props.get("name")) if isinstance(props, dict) else ""
    found = re.search(r"EPSG:+(\d+)$", name)
    if found:
        return int(found.group(1))
    if name.endswith("CRS84"):
        return 4326
    raise Refusal("the file has a crs member the tool cannot read: %r" % crs)


def raw_features(doc, path):
    """The feature dicts of a FeatureCollection or a single Feature."""
    kind = doc.get("type") if isinstance(doc, dict) else None
    if kind == "FeatureCollection":
        feats = doc.get("features")
    elif kind == "Feature":
        feats = [doc]
    else:
        raise ValueError("%s: not a GeoJSON FeatureCollection or Feature"
                         % path)
    if not isinstance(feats, list) or not all(isinstance(f, dict)
                                              for f in feats):
        raise ValueError("%s: features is not a list of objects" % path)
    return feats


def build_feats(raw, kind, key_field, label):
    """Turn feature dicts into Feat objects. kind is 'polygon' or 'line'.
    A feature with no geometry is counted, never hidden. A polygon that is
    not valid refuses the run, because every area derived from it is wrong."""
    feats, skipped = [], 0
    for i, f in enumerate(raw):
        if f.get("geometry") is None:
            skipped += 1
            continue
        try:
            geom = shapely.force_2d(shapely.geometry.shape(f["geometry"]))
        except Exception as exc:
            raise ValueError("%s: feature %d has a geometry shapely cannot "
                             "build: %s" % (label, i, exc))
        if geom.is_empty:
            skipped += 1
            continue
        props = f.get("properties")
        props = props if isinstance(props, dict) else {}
        key = props.get(key_field)
        key = "#%d" % i if key in (None, "") else str(key)
        if kind == "polygon":
            if geom.geom_type not in POLYGONS:
                raise Refusal("%s: feature %d is a %s, not a polygon"
                              % (label, i, geom.geom_type))
            if not geom.is_valid:
                raise Refusal("%s: polygon '%s' is not valid (%s). Every area "
                              "derived from it would be wrong; repair it first."
                              % (label, key, shapely.is_valid_reason(geom)))
            feats.append(Feat(len(feats), key, geom))
        elif geom.geom_type in ("LineString", "MultiLineString"):
            for part in shapely.get_parts(geom):
                feats.append(Feat(len(feats), key, part))
        else:
            raise Refusal("%s: feature %d is a %s, not a line"
                          % (label, i, geom.geom_type))
    return feats, skipped


def prepare_layer(path, epsg, work, label, kind, key_field):
    """Read one GeoJSON layer and bring it to the work CRS. Returns
    (feats, notes). OSError and ValueError mean the file could not be read."""
    with io.open(path, "r", encoding="utf-8") as fh:
        try:
            doc = json.load(fh)
        except ValueError as exc:
            raise ValueError("%s: %s" % (label, exc))
    declared = declared_epsg(doc) if isinstance(doc, dict) else None
    if declared is not None and declared != epsg:
        raise Refusal("%s: the file declares EPSG:%d and the flag says "
                      "EPSG:%d. Name the right one." % (label, declared, epsg))
    feats, skipped = build_feats(raw_features(doc, path), kind, key_field,
                                 label)
    notes = []
    if skipped:
        notes.append("%s: %d feature(s) with no geometry were skipped"
                     % (label, skipped))
    if feats:
        feats, note = convert_layer(feats, crs_facts(epsg), work, label)
        notes.append(note)
    return feats, notes


# ---------------------------------------------------------------- overlaps

def pair_area(ga, gb, tol, eps, clip=None):
    """Area shared by two polygons. Returns (area, remeasured).

    Both polygons are cut to the box their extents share before they are
    intersected. That is exact on paper, because nothing outside the box can
    lie in both. It is not bitwise identical on a machine, so an area within
    eps of the threshold is measured again on the uncut polygons, which makes
    the verdict the same by construction."""
    clip = clip or shapely.clip_by_rect
    a, b = ga.bounds, gb.bounds
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x0 >= x1 or y0 >= y1:
        return 0.0, False
    try:
        area = clip(ga, x0, y0, x1, y1).intersection(
            clip(gb, x0, y0, x1, y1)).area
    except shapely.errors.GEOSException:
        # clip_by_rect does not promise a valid result, and overlaying an
        # invalid one can raise. The uncut polygons were checked valid at load.
        return ga.intersection(gb).area, True
    if abs(area - tol) <= eps:
        return ga.intersection(gb).area, True
    return area, False


def find_overlaps(feats, tol, eps=CLIP_EPS, clip=None):
    """Pairs of polygons sharing area. Returns (findings, info)."""
    geoms = [f.geom for f in feats]
    tree = shapely.STRtree(geoms)
    items, pairs, remeasured = [], 0, 0
    for i, g in enumerate(geoms):
        for j in sorted(int(k) for k in tree.query(g, predicate="intersects")):
            if j <= i:
                continue
            pairs += 1
            area, again = pair_area(g, geoms[j], tol, eps, clip)
            remeasured += int(again)
            if area > 0.0:
                items.append((area, (i, j)))
    g = gate(items, tol)
    findings = []
    for status, group in ((REPORTED, g.kept), (SUPPRESSED, g.dropped)):
        for area, (i, j) in group:
            a, b = feats[i], feats[j]
            pt = geoms[i].intersection(geoms[j]).representative_point()
            if a.key == b.key:
                cls = "same-key"
                detail = ("two rows of '%s' overlap by %.4f sq m: usually one "
                          "area digitized twice, but two areas that share a "
                          "key look the same" % (a.key, area))
            else:
                cls = "overlap"
                detail = ("'%s' overlaps '%s' by %.4f sq m"
                          % (a.key, b.key, area))
            findings.append(Finding("overlap", status, cls, a.key, b.key,
                                    area, pt.x, pt.y, detail))
    findings.sort(key=lambda f: (f.status != REPORTED, -f.value))
    return findings, {"gate": g, "pairs": pairs, "remeasured": remeasured}


# -------------------------------------------------------------------- gaps

def areal_parts(geom):
    """The parts of geom that have area. A difference can come back empty or
    carry a stray line, and neither is a piece of uncovered ground."""
    return [p for p in shapely.get_parts(geom) if p.area > 0.0]


def find_gaps(feats, tol, min_width, mult=ENCLAVE_SUSPECT_MULT):
    """Holes in the dissolved coverage. Returns (findings, info).

    Two gates run in turn: area, then mean width. A hole drawn into one
    polygon, and bordered by no other, is an enclave and passes. A wide hole
    ringed by several polygons is a suspect enclave and fails, because only
    the first can be proved. A polygon set into another's hole leaves a ring
    that both border, so that ring is a gap."""
    geoms = [f.geom for f in feats]
    merged = shapely.unary_union(geoms)
    tree = shapely.STRtree(geoms)
    pieces = []
    for poly in shapely.get_parts(merged):
        for ring in poly.interiors:
            diff = shapely.Polygon(ring).difference(merged)
            pieces.extend(areal_parts(diff))
    by_area = gate([(p.area, p) for p in pieces], tol)
    widths = [(2.0 * p.area / p.length, (p, p.area)) for _, p in by_area.kept]
    by_width = gate(widths, min_width)
    findings = []

    def add(status, cls, key, other, piece, detail):
        pt = piece.representative_point()
        findings.append(Finding("gap", status, cls, key, other, piece.area,
                                pt.x, pt.y, detail))

    for area, p in by_area.dropped:
        add(SUPPRESSED, "below-area", "", "", p,
            "%.4f sq m hole is not over the %.4g sq m area gate" % (area, tol))
    for width, (p, area) in by_width.dropped:
        add(SUPPRESSED, "narrow", "", "", p,
            "%.4f sq m hole, mean width %.4f m, is not over the %.4g m "
            "width gate" % (area, width, min_width))
    wide_at = mult * max(min_width, DEFAULT_MIN_GAP_WIDTH)
    for width, (p, area) in by_width.kept:
        touching = set(int(k) for k in tree.query(p.buffer(BORDER_PAD))
                       if geoms[int(k)].distance(p) <= BORDER_PAD)
        owner = next(iter(touching)) if len(touching) == 1 else None
        shape = "%.4f sq m, mean width %.4f m" % (area, width)
        if owner is not None:
            add(INFO, "enclave", feats[owner].key, "", p,
                "%s: a hole drawn into '%s' itself, not ground left between "
                "polygons" % (shape, feats[owner].key))
            continue
        near = sorted(set(feats[k].key for k in touching))
        listed = ", ".join("'%s'" % k for k in near[:4])
        if len(near) > 4:
            listed += " and %d more" % (len(near) - 4)
        if len(near) > 1 and width > wide_at:
            add(REPORTED, "suspect-enclave", near[0], listed, p,
                "%s: wide uncovered area ringed by %d polygons (%s). Verify "
                "whether it is a deliberate enclave." % (shape, len(near),
                                                         listed))
        else:
            add(REPORTED, "gap", near[0] if near else "", listed, p,
                "%s: covered by no polygon; bordered by %s"
                % (shape, listed or "(none)"))
    findings.sort(key=lambda f: (f.status != REPORTED, f.status != INFO,
                                 -f.value))
    return findings, {"area": by_area, "width": by_width, "holes": len(pieces)}


# ---------------------------------------------------------------- snapgap

def endpoints_of(lines):
    """(line index, Point) for both ends of every line."""
    out = []
    for i, f in enumerate(lines):
        c = f.geom.coords
        out.append((i, shapely.Point(c[0])))
        out.append((i, shapely.Point(c[-1])))
    return out


def lands_on_own_body(line, at_start, node_tol):
    """Whether the chosen end of a line comes back to the line's own body, as
    the end of a P-shaped cul-de-sac drawn as one line does. The first
    segment, which holds the end, and any vertices within node_tol of it are
    left out, so the end does not match itself."""
    coords = list(line.coords)
    if not at_start:
        coords.reverse()
    end = shapely.Point(coords[0])
    first = 1
    while (first < len(coords)
           and end.distance(shapely.Point(coords[first])) <= node_tol):
        first += 1
    rest = coords[first:]
    if len(rest) < 2:
        return False
    return shapely.LineString(rest).distance(end) <= node_tol


def vet_boundary(boundary, ends, snap, min_cover):
    """Reasons a boundary layer cannot be trusted to suppress dangles. An
    empty list means it can. Each reason is checked against the street ends,
    because a boundary in the wrong place or the wrong CRS suppresses nothing
    and leaves every road end at the edge of the data in the report."""
    reasons = []
    if boundary.is_empty or boundary.area <= 0.0:
        return ["the boundary layer has no area"]
    pts = [p for _, p in ends]
    ebox = shapely.box(*shapely.total_bounds(pts))
    if not shapely.box(*boundary.bounds).intersects(ebox):
        reasons.append("the boundary layer does not overlap the street layer "
                       "at all; one of them is in the wrong CRS or the wrong "
                       "place")
    grown = boundary.buffer(snap)
    share = sum(1 for p in pts if grown.covers(p)) / float(len(pts))
    if share < min_cover:
        reasons.append("only %.0f%% of street ends lie inside the boundary "
                       "layer, under the %.0f%% required"
                       % (100.0 * share, 100.0 * min_cover))
    return reasons


def classify_dangles(lines, snap, isolated, node_tol, boundary=None):
    """Classify every street end that no other street end shares. Returns
    (findings, info).

    A node is every end within node_tol of another, found by single linkage,
    so a node's degree does not depend on where a rounding grid falls. An end
    within node_tol of another street's body is joined (a T junction) and is
    not a dangle. Then boundary, then fallout (a street within snap), then an
    isolated dead end, which carries the distance to what is nearest. An end
    outside the boundary layer is suppressed. An end inside it but within snap
    of its edge is suppressed only when no street is within snap, because a
    street that near makes it a possible gap the edge must not hide."""
    ends = endpoints_of(lines)
    pts = [p for _, p in ends]
    tree = shapely.STRtree(pts)
    parent = list(range(len(pts)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a, p in enumerate(pts):
        for b in tree.query(p.buffer(node_tol)):
            b = int(b)
            if b > a and p.distance(pts[b]) <= node_tol:
                parent[find(b)] = find(a)
    size = {}
    for a in range(len(pts)):
        size[find(a)] = size.get(find(a), 0) + 1
    search = max(snap, isolated)
    ltree = shapely.STRtree([f.geom for f in lines])
    edge = boundary.boundary if boundary is not None else None
    counts = {"ends": len(pts), "joined": 0, "boundary": 0, "fallout": 0,
              "isolated": 0, "shared": 0}
    findings, distances = [], []
    for a, (i, p) in enumerate(ends):
        if size[find(a)] > 1:
            counts["shared"] += 1
            continue
        near = sorted((p.distance(lines[int(j)].geom), int(j))
                      for j in ltree.query(p.buffer(search)) if int(j) != i)
        if (near and near[0][0] <= node_tol) or lands_on_own_body(
                lines[i].geom, a % 2 == 0, node_tol):
            counts["joined"] += 1
            continue
        key = lines[i].key
        if boundary is not None:
            outside = not boundary.covers(p)
            beside = bool(near) and near[0][0] <= snap
            if outside or (edge.distance(p) <= snap and not beside):
                counts["boundary"] += 1
                why = ("end lies outside the boundary layer, so it leaves "
                       "the area" if outside else
                       "end lies within %.4g m of the boundary edge, so it "
                       "is taken to leave the area" % snap)
                if beside:
                    why += ("; street '%s' is %.2f m away, so this may be a "
                            "gap that the boundary hides"
                            % (lines[near[0][1]].key, near[0][0]))
                findings.append(Finding(
                    "snapgap", SUPPRESSED, "boundary", key, "", None, p.x, p.y,
                    why))
                continue
        if near and near[0][0] <= search:
            d, j = near[0]
            other = lines[j].key
            distances.append(d)
            if d <= snap:
                counts["fallout"] += 1
                findings.append(Finding(
                    "snapgap", REPORTED, "fallout", key, other, d, p.x, p.y,
                    "end stops %.2f m from '%s' and is not joined to it"
                    % (d, other)))
            else:
                counts["isolated"] += 1
                findings.append(Finding(
                    "snapgap", INFO, "isolated", key, other, d, p.x, p.y,
                    "dead end; the nearest street '%s' is %.2f m away, "
                    "beyond the %.4g m snap tolerance" % (other, d, snap)))
        else:
            counts["isolated"] += 1
            findings.append(Finding(
                "snapgap", INFO, "isolated", key, "", None, p.x, p.y,
                "isolated dead end, nothing within %.4g m" % search))
    findings.sort(key=lambda f: (f.status != REPORTED, f.status != INFO,
                                 f.value if f.value is not None else 1e18))
    return findings, {"counts": counts, "distances": distances,
                      "snap": snap, "search": search}


def histogram(distances, width):
    """(low, high, count) for each non-empty bin of distances, lowest first."""
    bins = {}
    for d in distances:
        bins[int(d // width)] = bins.get(int(d // width), 0) + 1
    return [(k * width, (k + 1) * width, bins[k]) for k in sorted(bins)]


# ----------------------------------------------------------------- report

def describe(sections, notes, sample):
    """The text of a run. sections is a list of (title, header lines,
    findings)."""
    lines = list(notes)
    for title, header, findings in sections:
        lines.append("")
        lines.append(title)
        lines.extend(header)
        for status, label in ((REPORTED, "REPORTED"), (INFO, "INFO"),
                              (SUPPRESSED, "SUPPRESSED")):
            group = [f for f in findings if f.status == status]
            if not group:
                continue
            lines.append("  %s (%d)" % (label, len(group)))
            for f in group[:sample]:
                lines.append("    %s at (%.2f, %.2f): %s"
                             % (f.key or "-", f.x, f.y, f.detail))
            if len(group) > sample:
                lines.append("    ... %d more; --out lists them all"
                             % (len(group) - sample))
    return lines


def overlap_section(feats, tol, eps=CLIP_EPS):
    findings, info = find_overlaps(feats, tol, eps)
    header = ["  %d polygon(s), %d intersecting pair(s) compared"
              % (len(feats), info["pairs"])]
    header += gate_text("area", info["gate"], "sq m")
    header.append("  %d pair(s) within %.0e sq m of the threshold were "
                  "measured again on the uncut polygons"
                  % (info["remeasured"], eps))
    return ("OVERLAPS", header, findings)


def gap_section(feats, tol, min_width):
    findings, info = find_gaps(feats, tol, min_width)
    header = ["  %d uncovered hole(s) in the dissolved coverage"
              % info["holes"],
              "  only enclosed holes are checked; a strip that reaches the "
              "outer edge of the coverage is not found, so a clean result "
              "does not rule it out"]
    header += gate_text("area", info["area"], "sq m")
    header += gate_text("width", info["width"], "m")
    return ("GAPS", header, findings)


def snap_section(lines, args, boundary):
    findings, info = classify_dangles(lines, args.snap_tol, args.isolated_tol,
                                      args.node_tol, boundary)
    c = info["counts"]
    header = ["  %d street(s), %d end(s): %d shared by another street, %d "
              "joined to a street body, %d on or beyond the boundary, "
              "%d dangling"
              % (len(lines), c["ends"], c["shared"], c["joined"],
                 c["boundary"], c["fallout"] + c["isolated"]),
              "  snap tolerance %.4g m, searched out to %.4g m; node "
              "tolerance %.4g m" % (info["snap"], info["search"],
                                    args.node_tol)]
    if boundary is None:
        header.append("  no boundary layer: ends at the edge of the data are "
                      "not suppressed")
    bins = histogram(info["distances"], args.bin)
    if bins:
        top = max(bins, key=lambda b: (b[2], -b[0]))
        header.append("  distance from a dangling end to the nearest street:")
        for lo, hi, n in bins:
            mark = ""
            if (lo, hi, n) == top and n >= MIN_CLUSTER:
                mark = "  <-- densest"
            header.append("    %6.1f - %6.1f m  %3d  %s%s"
                          % (lo, hi, n, "#" * min(n, 40), mark))
    return ("SNAPGAP", header, findings)


def gate_exit(sections):
    """1 when any finding is REPORTED, else 0."""
    failed = any(f.status == REPORTED for _, _, fs in sections for f in fs)
    return EXIT_GATE if failed else EXIT_OK


def write_findings(path, sections):
    """Write every finding, reported or not, so a suppression can be audited."""
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(OUT_COLUMNS)
    for _, _, findings in sections:
        for f in findings:
            w.writerow(f.row())
    # Encode before opening, so a key that UTF-8 cannot hold (a lone
    # surrogate) fails with the existing file untouched, not half written.
    data = buf.getvalue().encode("utf-8")
    with io.open(path, "wb") as fh:
        fh.write(data)


# --------------------------------------------------------------- self-test
class _Harness(object):
    """Counts assertions, prints each one, and reports failures by name."""

    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, cond, label):
        if cond:
            self.passed += 1
            print("PASS  %s" % label)
        else:
            self.failed.append(label)
            print("FAIL  %s" % label)

    def meta(self, cond, label):
        """Check the harness itself without calling check(), so a check()
        that always passes cannot vouch for itself."""
        if cond:
            self.passed += 1
            print("PASS  %s" % label)
        else:
            self.failed.append(label)
            print("FAIL  %s" % label)

    def raises(self, fn, exc, label):
        try:
            fn()
        except exc:
            self.check(True, label)
        except Exception as other:
            self.check(False, "%s (wrong exception %r)" % (label, other))
        else:
            self.check(False, "%s (no error raised)" % label)

    def footer(self):
        total = self.passed + len(self.failed)
        print("-" * 68)
        if self.failed:
            print("%d assertions, %d failed" % (total, len(self.failed)))
            for label in self.failed:
                print("FAILED: %s" % label)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0


def self_test():
    """Run the assertions in a temp directory that is removed even when one
    of them raises, so a crash never leaves ng911topo-selftest-* behind."""
    load_libs()
    tmp = tempfile.mkdtemp(prefix="ng911topo-selftest-")
    try:
        return _self_test(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _self_test(tmp):
    """Assertions over the decision core and the CLI. No network. The only
    files are the ones written under tmp."""
    h = _Harness()
    check, raises = h.check, h.raises
    box = shapely.box
    meta = h.meta

    def feats_of(pairs):
        return [Feat(i, k, g) for i, (k, g) in enumerate(pairs)]

    def at(findings, x, y):
        for f in findings:
            if abs(f.x - x) < 1e-6 and abs(f.y - y) < 1e-6:
                return f
        return None

    def run(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def write_json(name, features, crs=None):
        path = os.path.join(tmp, name)
        doc = {"type": "FeatureCollection", "features": features}
        if crs:
            doc["crs"] = crs
        with io.open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return path

    def pfeat(key, geom):
        return {"type": "Feature", "properties": {"KEY": key},
                "geometry": shapely.geometry.mapping(geom)}

    def lfeat(key, coords):
        return {"type": "Feature", "properties": {"SEGMENT_ID": key},
                "geometry": {"type": "LineString", "coordinates": coords}}

    print("ng911topo self-test: temporary files only, no network")
    print("-" * 68)

    # ---- the gate: what it keeps, what it removes, what sits either side
    g = gate([(0.5, "a"), (1.0, "b"), (1.5, "c"), (9.0, "d")], 1.0)
    check([i for _, i in g.kept] == ["c", "d"],
          "a value must exceed the threshold to be kept")
    check([i for _, i in g.dropped] == ["a", "b"] and g.largest_dropped == 1.0,
          "a value exactly on the threshold is dropped on every gate  <-- pinned defect")
    check(g.nearest_above == 1.5,
          "the nearest case above the threshold is reported")
    empty = gate([], 1.0)
    check(empty.nearest_above is None and empty.largest_dropped is None,
          "an empty gate has no nearest case and no largest drop")
    text = gate_text("area", g, "sq m")
    check(text[0].endswith("must exceed 1 sq m")
          and "2 over the threshold, 2 suppressed (largest suppressed 1 sq m)" in text[1]
          and "nearest case above the threshold: 1.5 sq m" in text[2],
          "the gate text states the threshold, the suppressed count and the nearest case  <-- pinned defect")
    text = gate_text("width", gate([(0.1, "x")], 1.0), "m")
    check("0 over the threshold, 1 suppressed" in text[1] and text[2] == "  no case above the threshold",
          "a gate that kept nothing says so")
    text = gate_text("width", gate([(2.0, "x")], 1.0), "m")
    check("largest" not in text[1], "a gate that dropped nothing names no largest drop")

    # ---- the clip: exact on paper, measured against the uncut polygons
    ab = (box(0, 0, 10, 10), box(5, 0, 15, 10))
    check(pair_area(ab[0], ab[1], 1.0, 1e-6) == (50.0, False),
          "two overlapping squares share 50 sq m")
    check(pair_area(box(0, 0, 10, 10), box(10, 0, 20, 10), 1.0, 1e-6) == (0.0, False),
          "two squares that share only an edge share no area")
    check(pair_area(box(0, 0, 10, 10), box(5, 20, 15, 30), 1.0, 1e-6) == (0.0, False),
          "two squares whose extents do not meet share no area")
    rnd = random.Random(20261022)

    def star(cx, cy):
        # Evenly spaced angles and a smooth radius keep the ring simple.
        k, phase = rnd.randint(2, 7), rnd.uniform(0.0, 6.0)
        return shapely.Polygon([
            (cx + (30.0 + 8.0 * math.sin(k * a + phase)) * math.cos(a),
             cy + (30.0 + 8.0 * math.sin(k * a + phase)) * math.sin(a))
            for a in [2.0 * math.pi * n / 120.0 for n in range(120)]])

    worst, compared, valid = 0.0, 0, True
    for _ in range(40):
        pa, pb = star(0.0, 0.0), star(rnd.uniform(-30, 30), rnd.uniform(-30, 30))
        valid = valid and pa.is_valid and pb.is_valid
        full = pa.intersection(pb).area
        cut, _ = pair_area(pa, pb, 1.0, -1.0)
        worst = max(worst, abs(cut - full))
        compared += int(full > 0.0)
    print("info: clipped and uncut areas differ by at most %.3g sq m over "
          "%d overlapping random pairs" % (worst, compared))
    check(valid and compared >= 20,
          "the random polygons are valid and most of them really overlap")
    check(worst < CLIP_EPS,
          "the clipped area differs from the uncut area by less than the re-measure margin")

    def nudged(geom, x0, y0, x1, y1):
        # A clip that loses 2.5e-7 sq m: far below any real threshold, and
        # enough to flip a verdict that sits a hair under it.
        return shapely.clip_by_rect(geom, x0, y0, x1 - 2.5e-8, y1)

    tol = 50.0 - 1e-7
    flipped = pair_area(ab[0], ab[1], tol, 0.0, nudged)
    check(flipped[0] < tol and ab[0].intersection(ab[1]).area > tol,
          "without the re-measure an area a hair under the threshold flips the verdict  <-- pinned defect")
    safe = pair_area(ab[0], ab[1], tol, 1e-6, nudged)
    check(safe == (50.0, True),
          "an area within the margin of the threshold is measured again on the uncut polygons  <-- pinned defect")
    def broken(geom, x0, y0, x1, y1):
        raise shapely.errors.GEOSException("clip gave an invalid polygon")

    check(pair_area(ab[0], ab[1], 40.0, 1e-6, broken) == (50.0, True),
          "a clip that fails falls back to the uncut polygons instead of crashing  <-- pinned defect")
    far = pair_area(ab[0], ab[1], 40.0, 1e-6, nudged)
    check(far[1] is False and abs(far[0] - 50.0) < 1e-6,
          "an area far from the threshold is not measured again")

    # ---- overlaps
    polys = feats_of([
        ("A", box(0, 0, 10, 10)), ("B", box(5, 0, 15, 10)),
        ("C", box(15, 0, 25, 10)), ("D", box(100, 100, 110, 110)),
        ("F", box(200, 0, 210, 10)), ("F", box(205, 0, 215, 10)),
        ("G", box(300, 0, 310, 10)), ("H", box(309.95, 0, 320, 10)),
        ("I", box(400, 0, 410, 10)), ("J", box(409.85, 0, 420, 10))])
    found, info = find_overlaps(polys, 1.0)
    rep = [f for f in found if f.status == REPORTED]
    sup = [f for f in found if f.status == SUPPRESSED]
    check(sorted((f.key, f.other) for f in rep)
          == [("A", "B"), ("F", "F"), ("I", "J")],
          "overlaps over the threshold are reported once per pair")
    check(len(found) == 4 and info["pairs"] == 5,
          "squares that only share an edge are compared and are not findings")
    check(len(sup) == 1 and sup[0].key == "G" and abs(sup[0].value - 0.5) < 1e-6,
          "a sliver under the threshold is kept as a suppressed finding, not lost  <-- pinned defect")
    check(abs(info["gate"].nearest_above - 1.5) < 1e-6
          and abs(info["gate"].largest_dropped - 0.5) < 1e-6,
          "the gate reports the case just above the threshold and the largest it dropped  <-- pinned defect")
    same = [f for f in rep if f.cls == "same-key"][0]
    check("digitized twice" in same.detail and "share a key" in same.detail,
          "two rows of one key are named as probably one area digitized twice, with the other cause stated  <-- pinned defect")
    first = [f for f in rep if f.key == "A"][0]
    check(5.0 <= first.x <= 10.0 and 0.0 <= first.y <= 10.0,
          "a finding sits inside the overlap so an analyst can zoom to it")
    check(find_overlaps(polys[:1], 1.0)[1]["pairs"] == 0,
          "one polygon has no pair to compare and the run says so")
    over = find_overlaps(feats_of([("A", box(0, 0, 10, 10)),
                                   ("B", box(5, 0, 15, 10))]), 50.0)[0]
    check(len(over) == 1 and over[0].status == SUPPRESSED,
          "an overlap exactly at the threshold is suppressed")
    check(pair_area(ab[0], ab[1], 50.0, 0.0) == (50.0, True)
          and pair_area(ab[0], ab[1], 49.0, 0.0) == (50.0, False),
          "an area exactly eps from the threshold is measured again, and one beyond it is not  <-- pinned defect")
    check(info["remeasured"] == 0
          and find_overlaps(feats_of([("A", box(0, 0, 10, 10)), ("B", box(5, 0, 15, 10))]),
                            50.0)[1]["remeasured"] == 1
          and find_overlaps(feats_of([("A", box(0, 0, 10, 10)), ("B", box(9.895, 0, 20, 10))]),
                            1.0)[1]["remeasured"] == 0,
          "the count of pairs measured again is counted, and a pair 0.05 sq m from the threshold is not one  <-- pinned defect")
    ladder = find_overlaps(feats_of([
        ("a", box(0, 0, 10, 10)), ("b", box(8, 0, 18, 10)),
        ("c", box(100, 0, 110, 10)), ("d", box(102, 0, 112, 10)),
        ("e", box(200, 0, 210, 10)), ("f", box(205, 0, 215, 10)),
        ("g", box(300, 0, 310, 10)), ("h", box(309.95, 0, 320, 10))]), 1.0)[0]
    check([round(f.value, 1) for f in ladder] == [80.0, 50.0, 20.0, 0.5],
          "findings list the largest overlap first, then the suppressed ones  <-- pinned defect")

    # ---- gaps: one fixture per outcome
    mixed = shapely.GeometryCollection([shapely.LineString([(0, 0), (5, 5)]),
                                        box(0, 0, 2, 2)])
    check(areal_parts(shapely.Polygon()) == [] and areal_parts(mixed) == [box(0, 0, 2, 2)],
          "an empty difference and a stray line are not pieces of uncovered ground")
    def frame(tag, ox, w, h):
        return [(tag + "L", box(ox - 10, -10, ox, h + 10)),
                (tag + "R", box(ox + w, -10, ox + w + 10, h + 10)),
                (tag + "B", box(ox, -10, ox + w, 0)),
                (tag + "T", box(ox, h, ox + w, h + 10))]

    ring = shapely.Polygon(box(4000, 0, 4040, 40).exterior.coords,
                           [box(4015, 15, 4025, 25).exterior.coords])
    ring2 = shapely.Polygon(box(5000, 0, 5040, 40).exterior.coords,
                            [box(5015, 15, 5025, 25).exterior.coords])
    cover = (frame("g", 10, 2, 20) + frame("s", 1010, 10, 10)
             + frame("c", 2010, 0.1, 20) + frame("t", 3010, 0.5, 0.5)
             + [("E", ring), ("N", ring2), ("n", box(5019, 19, 5021, 21))])
    gfound, ginfo = find_gaps(feats_of(cover), 1.0, 0.15)
    by_cls = {}
    for f in gfound:
        by_cls.setdefault(f.cls, []).append(f)
    check(sorted(by_cls) == ["below-area", "enclave", "gap", "narrow",
                             "suspect-enclave"] and ginfo["holes"] == 6,
          "six holes land in five classes: gap, suspect enclave, enclave, narrow and below the area gate")
    gap = [f for f in by_cls["gap"] if f.key == "gB"][0]
    check(gap.status == REPORTED and abs(gap.value - 40.0) < 1e-6
          and gap.key == "gB" and gap.other == "'gB', 'gL', 'gR', 'gT'",
          "a real gap is reported with its area and every polygon that borders it")
    sus = by_cls["suspect-enclave"][0]
    check(sus.status == REPORTED and "Verify" in sus.detail,
          "a wide hole ringed by several polygons is a suspect enclave and fails the gate")
    check(len(by_cls["enclave"]) == 1 and by_cls["enclave"][0].key == "E"
          and by_cls["enclave"][0].status == INFO,
          "a hole drawn into one polygon and bordered by no other is an enclave and passes the gate")
    nested = [f for f in by_cls["suspect-enclave"] if f.key == "N"][0]
    check(abs(nested.value - 96.0) < 1e-6 and nested.status == REPORTED
          and nested.other == "'N', 'n'",
          "a polygon nested in a hole is not counted as uncovered ground, and the ring left around it is reported, not passed as an enclave  <-- pinned defect")
    check(by_cls["narrow"][0].status == SUPPRESSED
          and by_cls["below-area"][0].status == SUPPRESSED,
          "a crack and a speck are kept as suppressed findings, not lost  <-- pinned defect")
    check(abs(ginfo["area"].nearest_above - 2.0) < 1e-6
          and abs(ginfo["area"].largest_dropped - 0.25) < 1e-6
          and abs(ginfo["width"].largest_dropped - 0.0995) < 1e-3,
          "both gaps report the nearest case above and the largest case dropped")
    channel = frame("w", 10, 10, 10)[:3]
    check(find_gaps(feats_of(channel), 1.0, 0.15)[1]["holes"] == 0,
          "an open channel that reaches the outer edge is not a hole")
    big = frame("h", 10, 10, 10)[:2] + [
        ("hB1", box(10, -10, 15, 0)), ("hB2", box(15, -10, 20, 0)),
        ("hT1", box(10, 10, 15, 20)), ("hT2", box(15, 10, 20, 20))]
    bigf, _ = find_gaps(feats_of(big), 1.0, 0.15)
    check(len(bigf) == 1 and bigf[0].other.endswith("and 2 more"),
          "a hole ringed by six polygons names four and counts the rest")
    apart = find_gaps(feats_of([("a", box(0, 0, 10, 10)),
                                ("b", box(20, 0, 30, 10))]), 1.0, 0.15)[0]
    check(apart == [], "two polygons with open ground between them leave no interior hole")
    low = find_gaps(feats_of(frame("u", 10, 2, 20)), 1.0, 0.15, mult=0.0)[0]
    check(low[0].cls == "suspect-enclave",
          "the suspect-enclave multiplier is a parameter")
    zero_gate = find_gaps(feats_of(frame("z", 10, 0.1, 20)), 1.0, 0.0)[0]
    check(zero_gate[0].cls == "gap" and zero_gate[0].status == REPORTED,
          "a width gate of 0 reports every hole that clears the area gate")
    lone = find_gaps(feats_of([("L", shapely.Polygon(
        box(0, 0, 40, 40).exterior.coords, [box(10, 10, 12, 12).exterior.coords])),
        ("far", box(500, 500, 510, 510))]), 1.0, 0.15)[0]
    check(lone[0].cls == "enclave" and lone[0].other == "",
          "a hole in one polygon is an enclave even when other polygons exist elsewhere")
    donut = shapely.Polygon(box(0, 0, 60, 60).exterior.coords,
                            [box(10, 10, 50, 50).exterior.coords])
    short = find_gaps(feats_of([("A", donut), ("B", box(11, 11, 49, 49))]), 1.0, 0.15)[0]
    check(len(short) == 1 and short[0].cls == "gap" and short[0].status == REPORTED
          and short[0].other == "'A', 'B'" and abs(short[0].value - 156.0) < 1e-6,
          "a polygon set into a hole with a 1 m margin leaves a gap, not a deliberate enclave  <-- pinned defect")
    half = find_gaps(feats_of([("A", donut), ("B", box(10, 10, 30, 50))]), 1.0, 0.15)[0]
    check(len(half) == 1 and half[0].cls == "suspect-enclave" and half[0].status == REPORTED
          and abs(half[0].value - 800.0) < 1e-6,
          "a polygon that fills half of another's hole leaves a wide area that is reported  <-- pinned defect")
    fits = find_gaps(feats_of([("A", donut), ("B", box(10, 10, 50, 50))]), 1.0, 0.15)
    check(fits[0] == [] and fits[1]["holes"] == 0,
          "a polygon that fills another's hole exactly leaves nothing to report")
    vals = [f.value for f in gfound if f.status == REPORTED]
    check(len(vals) == 3 and vals == sorted(vals, reverse=True) and abs(vals[0] - 100.0) < 1e-6,
          "reported gaps list the largest first  <-- pinned defect")
    speck = find_gaps(feats_of(frame("q", 10, 1.5, 0.5)), 1.0, 0.15)[0]
    check(len(speck) == 1 and speck[0].cls == "below-area",
          "a hole of 0.75 sq m is under a 1 sq m area gate")
    edge_w = find_gaps(feats_of(frame("e", 10, 6, 6)), 1.0, 0.15)[0]
    check(len(edge_w) == 1 and edge_w[0].cls == "gap" and abs(edge_w[0].value - 36.0) < 1e-6,
          "a hole whose mean width is exactly 20 times the width gate is a gap, not a suspect enclave")
    ringed5 = find_gaps(feats_of(frame("r", 10, 10, 10)[:3] + [
        ("rT1", box(10, 10, 15, 20)), ("rT2", box(15, 10, 20, 20))]), 1.0, 0.15)[0]
    check(len(ringed5) == 1 and ringed5[0].other == "'rB', 'rL', 'rR', 'rT1' and 1 more",
          "a hole ringed by five polygons names four and counts one more  <-- pinned defect")
    twins = find_gaps(feats_of([(u"Z", b) for _, b in frame("r", 10, 10, 10)]), 1.0, 0.15)[0]
    check(len(twins) == 1 and twins[0].cls == "gap" and twins[0].other == "'Z'",
          "a wide hole ringed by rows that all share one key is a gap, since only one area can be named")
    walled = shapely.Polygon(box(0, 0, 30, 30).exterior.coords, [box(2, 2, 20, 20).exterior.coords])
    away = find_gaps(feats_of([("A", walled), ("Z", box(-10, 0, 0, 30))]), 1.0, 0.15)[0]
    check(len(away) == 1 and away[0].cls == "enclave" and away[0].status == INFO,
          "a polygon 2 m from a hole does not border it, so the hole is still an enclave  <-- pinned defect")

    # ---- snapgap: dangles
    def classify(pairs, snap=9.0, isolated=9.0, node=0.05, boundary=None):
        lines = feats_of([(k, shapely.LineString(c)) for k, c in pairs])
        return classify_dangles(lines, snap, isolated, node, boundary)

    out, info = classify([("A", [(0, 0), (100, 0)])])
    c = info["counts"]
    check(c["isolated"] == 2 and c["fallout"] == 0 and c["ends"] == 2
          and all(f.status == INFO and "nothing within 9 m" in f.detail for f in out),
          "a lone street has two isolated ends and no fallout")
    out, info = classify([("A", [(0, 0), (100, 0)]), ("B", [(50, 5), (50, 60)])])
    hit = at(out, 50, 5)
    check(hit.cls == "fallout" and hit.status == REPORTED and hit.other == "A"
          and abs(hit.value - 5.0) < 1e-9,
          "a stub that stops 5 m short of a road's middle is fallout and names the road")
    check(info["counts"]["fallout"] == 1 and info["counts"]["isolated"] == 3,
          "the other three ends are isolated dead ends")
    check(out[0].status == REPORTED, "reported findings sort before informational ones")

    lines = [("T", [(0, 0), (1000, 0)])]
    for n, d in enumerate((6.5, 7.0, 7.2, 7.6, 1.0)):
        x = 100 * (n + 1)
        lines.append(("S%d" % (n + 1), [(x, d), (x, 50)]))
    out, info = classify(lines)
    bins = histogram(info["distances"], 2.0)
    check(bins == [(0.0, 2.0, 1), (6.0, 8.0, 4)],
          "fallout distances fall into bins, so a cluster of undershoots stands out  <-- pinned defect")
    check(len(info["distances"]) == 5 and info["counts"]["fallout"] == 5,
          "five stubs within the snap tolerance are all fallout")

    def legacy_key(x, y):
        return (round(x, 1), round(y, 1))

    check(legacy_key(10.0499, 0.0) != legacy_key(10.0501, 0.0),
          "two ends 0.0002 m apart land in different cells of a 0.1 rounding grid")
    out, info = classify([("A", [(0, 0), (10.0499, 0)]),
                          ("B", [(10.0501, 0), (30, 0)])])
    c = info["counts"]
    check(c["shared"] == 2 and c["isolated"] == 2 and c["fallout"] == 0,
          "two ends 0.0002 m apart are one node, not two false dangles  <-- pinned defect")
    check(at(out, 10.0499, 0) is None and at(out, 10.0501, 0) is None,
          "the straddling ends are not reported at all")
    out, info = classify([("A", [(0, 0), (100, 0)]), ("B", [(50, 0.01), (50, 40)])])
    check(info["counts"]["joined"] == 1 and at(out, 50, 0.01) is None,
          "an end on another street's body is a joined T junction, not a dangle  <-- pinned defect")
    out, info = classify([("R", [(0, 0), (10, 0), (10, 10), (0, 0)])])
    check(info["counts"]["shared"] == 2 and out == [],
          "a closed street shares both its ends with itself")
    out, info = classify([("A", [(0, 0), (100, 0)]), ("B", [(112, 0), (200, 0)])],
                         snap=10.0, isolated=15.0)
    near = at(out, 100, 0)
    check(near.cls == "isolated" and near.status == INFO and near.other == "B"
          and abs(near.value - 12.0) < 1e-9 and "12.00 m away" in near.detail,
          "a dead end between the snap tolerance and the search radius states the real distance")
    check(at(out, 0, 0).value is None and "nothing within 15 m" in at(out, 0, 0).detail,
          "the radius quoted for an isolated end is the one actually searched")
    pair20 = [("A", [(0, 0), (100, 0)]), ("B", [(120, 0), (200, 0)])]
    check(at(classify(pair20, snap=30.0)[0], 100, 0).cls == "fallout"
          and at(classify(pair20, snap=10.0)[0], 100, 0).cls == "isolated",
          "the snap tolerance moves a 20 m gap between fallout and isolated")

    # ---- snapgap: a value exactly on a tolerance
    c = classify([("A", [(0, 0), (0, 100)]), ("B", [(0.05, 0), (0.05, -100)])])[1]["counts"]
    check(c["shared"] == 2 and c["joined"] == 0,
          "two ends exactly one node tolerance apart are one node  <-- pinned defect")
    c = classify([("A", [(-50, 0), (50, 0)]), ("B", [(0, 0.05), (0, 100)])])[1]["counts"]
    check(c["joined"] == 1 and c["fallout"] == 0,
          "an end exactly one node tolerance from another street's body is joined  <-- pinned defect")
    out, _ = classify([("A", [(0, 0), (-100, 0)]), ("B", [(0.04, 0.04), (100, 100)])])
    check(getattr(at(out, 0, 0), "cls", None) == "fallout"
          and getattr(at(out, 0.04, 0.04), "cls", None) == "fallout",
          "two ends in each other's bounding box but 0.057 m apart are two nodes, not one  <-- pinned defect")
    out, info = classify([("L", [(0, 0), (50, 0), (50, 20), (70, 20), (70, 0), (50, 0)]),
                          ("K", [(50, -4), (200, -4)])])
    check(at(out, 50, 0) is None and info["counts"]["joined"] == 1
          and getattr(at(out, 50, -4), "cls", None) == "fallout",
          "an end that lands on its own line's body is joined, so a lollipop is not fallout against its neighbour  <-- pinned defect")
    out, info = classify([("L", [(50, 0), (70, 0), (70, 20), (50, 20), (50, -10)])])
    check(at(out, 50, 0) is None and info["counts"]["joined"] == 1
          and getattr(at(out, 50, -10), "cls", None) == "isolated",
          "a start that lands on its own line's body is joined as an end is  <-- pinned defect")
    out, info = classify([("D", [(0, 0), (0.01, 0), (100, 0)])])
    check(info["counts"]["joined"] == 0 and info["counts"]["isolated"] == 2,
          "a repeated first vertex does not make the start land on its own body  <-- pinned defect")
    out, _ = classify([("A", [(0, 0), (100, 0)]), ("B", [(50, 9), (50, 60)])])
    check(at(out, 50, 9).cls == "fallout" and at(out, 50, 9).status == REPORTED,
          "an end exactly on the snap tolerance is fallout  <-- pinned defect")
    out, _ = classify([("A", [(0, 0), (100, 0)]), ("B", [(50, 20), (50, 60)])],
                      snap=9.0, isolated=20.0)
    check(at(out, 50, 20).cls == "isolated" and at(out, 50, 20).other == "A",
          "an end exactly at the search radius is isolated and still names its nearest street  <-- pinned defect")
    out, _ = classify([("T", [(0, 0), (1000, 0)]), ("S1", [(100, 7), (100, 50)]),
                       ("S2", [(200, 3), (200, 50)]), ("S3", [(300, 5), (300, 50)])])
    check([f.value for f in out if f.status == REPORTED] == [3.0, 5.0, 7.0],
          "fallout lists the nearest stop first  <-- pinned defect")

    # ---- snapgap: the boundary layer
    county = box(0, 0, 1000, 1000)
    bl = [("O", [(500, 500), (500, 1100)]), ("E", [(700, 10), (700, 300)]),
          ("W", [(400, 25), (600, 25)]), ("V", [(500, 20), (500, 400)]),
          ("F", [(300, 500), (900, 500)]), ("G", [(800, 505), (800, 900)])]
    out, info = classify(bl, snap=30.0, isolated=30.0, boundary=county)
    check(at(out, 500, 1100).cls == "boundary" and at(out, 500, 1100).status == SUPPRESSED,
          "an end beyond the boundary layer is suppressed, and kept as a suppressed finding")
    check(at(out, 700, 10).cls == "boundary",
          "an end inside the boundary layer but within the snap tolerance of its edge is suppressed")
    check(at(out, 500, 20).cls == "fallout" and at(out, 500, 20).status == REPORTED
          and at(out, 500, 20).other == "W",
          "an end inside the boundary layer, within the snap tolerance of its edge and of another street, is fallout and fails the gate  <-- pinned defect")
    out_near, _ = classify([("X", [(950, 1005), (950, 1200)]),
                            ("Y", [(930, 900), (930, 1002)])],
                           snap=30.0, isolated=30.0, boundary=county)
    check(at(out_near, 950, 1005).cls == "boundary"
          and "street 'Y' is 20.22 m away" in at(out_near, 950, 1005).detail
          and "street" not in at(out, 500, 1100).detail.replace("street ends", ""),
          "an end outside the boundary layer stays suppressed, and names a street inside the snap tolerance so the hidden gap is visible")
    check(at(out, 800, 505).cls == "fallout" and at(out, 800, 505).status == REPORTED,
          "an interior fallout is not touched by the boundary layer")
    check(at(out, 300, 500).cls == "isolated" and info["counts"]["boundary"] == 4,
          "an interior dead end stays isolated with a boundary layer present")
    check("lies outside the boundary layer" in at(out, 500, 1100).detail
          and "within 30 m of the boundary edge" in at(out, 700, 10).detail,
          "a suppressed end says whether it lies outside the boundary or only near its edge  <-- pinned defect")
    out, _ = classify(bl, snap=30.0, isolated=30.0)
    check(at(out, 500, 1100).cls == "isolated" and at(out, 700, 10).cls == "isolated",
          "without a boundary layer the same ends are listed as dead ends")
    out, _ = classify([("P", [(500, 30), (500, 400)]), ("Q", [(600, 31), (600, 400)])],
                      snap=30.0, isolated=30.0, boundary=county)
    check(at(out, 500, 30).cls == "boundary" and at(out, 600, 31).cls == "isolated",
          "an end exactly one snap tolerance from the boundary edge is suppressed, and one 1 m further in is not  <-- pinned defect")

    # ---- the boundary layer is refused, not silently dropped
    ends = endpoints_of(feats_of([(k, shapely.LineString(c)) for k, c in bl]))
    check(vet_boundary(county, ends, 30.0, 0.5) == [],
          "a boundary layer that holds the streets is trusted")
    away = shapely.box(1000000, 1000000, 1001000, 1001000)
    reasons = vet_boundary(away, ends, 30.0, 0.5)
    check(len(reasons) == 2 and "does not overlap" in reasons[0]
          and "only 0%" in reasons[1],
          "a boundary layer in the wrong place or CRS is refused with both reasons  <-- pinned defect")
    small = vet_boundary(box(0, 0, 400, 400), ends, 30.0, 0.5)
    check(len(small) == 1 and small[0].startswith("only"),
          "a boundary layer that holds too few street ends is refused")
    check(vet_boundary(shapely.Polygon(), ends, 30.0, 0.5)
          == ["the boundary layer has no area"],
          "an empty boundary layer is refused")
    check(vet_boundary(shapely.LineString([(0, 0), (1, 0)]), ends, 30.0, 0.5)
          == ["the boundary layer has no area"],
          "a boundary layer with no area is refused")
    vends = endpoints_of(feats_of([
        ("p", shapely.LineString([(50, 50), (60, 50)])),
        ("q", shapely.LineString([(105, 50), (110, 50)]))]))
    sq = box(0, 0, 100, 100)
    check(vet_boundary(sq, vends, 9.0, 0.75) == []
          and len(vet_boundary(sq, vends, 0.0, 0.75)) == 1
          and len(vet_boundary(sq, vends, 9.0, 0.76)) == 1,
          "street ends within the snap tolerance of the boundary count as inside it, and a share exactly at the minimum passes  <-- pinned defect")

    # ---- the CRS: facts, work CRS, area of use, and the reprojection proof
    f26917, f2237, f4269 = crs_facts(26917), crs_facts(2237), crs_facts(4269)
    check(f26917["projected"] and f26917["factors"] == (1.0, 1.0)
          and f2237["unit"] == "US survey foot"
          and abs(f2237["factors"][0] - 0.3048006096) < 1e-9
          and f4269["geographic"] and not f4269["projected"],
          "PROJ reports a metre CRS, a foot CRS and a geographic CRS for what they are")
    raises(lambda: crs_facts(999999), Refusal,
           "an EPSG code PROJ does not know is refused")
    check(work_crs_problem(f26917) is None
          and "not a projected CRS" in work_crs_problem(f4269)
          and "US survey foot" in work_crs_problem(f2237),
          "only a projected metre CRS can be the work CRS  <-- pinned defect")
    inside = (-82.5, 28.5, -82.0, 29.0)
    check(aou_verdict(f2237["aou"], inside, 0.1) == (None, None),
          "data inside the area of use of its CRS passes")
    problem, _ = aou_verdict(f2237["aou"], (10.0, 50.0, 11.0, 51.0), 0.1)
    check("probably wrong" in problem,
          "data outside the area of use of its CRS is refused as a probable wrong tag  <-- pinned defect")
    check(aou_verdict(f4269["aou"], inside, 0.1) == (None, None)
          and aou_verdict(f4269["aou"], (100.0, 28.5, 101.0, 29.0), 0.1)[0] is not None,
          "an area of use that crosses the antimeridian is read as two spans, not as an empty one  <-- pinned defect")
    nan = float("nan")
    check(aou_verdict(f2237["aou"], (nan, 28.5, -82.0, 29.0), 0.1)[0] is not None,
          "a nan extent is refused, not passed  <-- pinned defect")
    check(aou_verdict(None, inside, 0.1)[1] is not None,
          "a CRS with no area of use is passed but says the tag is unchecked")
    box_aou = (10.0, 20.0, 30.0, 40.0)
    check(aou_verdict(box_aou, (9.0, 19.0, 31.0, 41.0), 1.0) == (None, None),
          "data exactly one margin outside the area of use on every side still passes  <-- pinned defect")
    check(all(aou_verdict(box_aou, b, 1.0)[0] is not None
              for b in ((8.9, 20.0, 30.0, 40.0), (10.0, 20.0, 31.1, 40.0),
                        (10.0, 18.9, 30.0, 40.0), (10.0, 20.0, 30.0, 41.1))),
          "data beyond the margin on any one side is refused  <-- pinned defect")
    check(aou_verdict((10.0, 0.0, 10.0, 40.0), (20.0, 5.0, 20.0, 6.0), 1.0)[0] is not None,
          "an area of use with equal west and east is not read as crossing the antimeridian")
    anti = (170.0, 0.0, -170.0, 80.0)
    check(aou_verdict(anti, (175.0, 5.0, -175.0, 6.0), 1.0) == (None, None)
          and aou_verdict(anti, (175.0, 5.0, 0.0, 6.0), 1.0)[0] is not None,
          "data that only partly falls in an antimeridian area of use is refused  <-- pinned defect")
    check(aou_verdict(anti, (169.5, 5.0, -169.5, 6.0), 1.0) == (None, None)
          and aou_verdict(anti, (168.5, 5.0, -175.0, 6.0), 1.0)[0] is not None
          and aou_verdict(anti, (175.0, 5.0, -168.5, 6.0), 1.0)[0] is not None,
          "an antimeridian area of use grows by the margin on both of its sides  <-- pinned defect")
    check(prove_reprojection(True, 0.0174532925, (-82.0, 29.0), (-81.0, 28.0),
                             (400.0, 20.0), (401.0, 21.0))[0] is True
          and prove_reprojection(True, 0.0174532925, (-82.0, 29.0), (-81.0, 28.0),
                                 (400000.0, 5.0), (400100.0, 6.0))[0] is True
          and prove_reprojection(True, 0.0174532925, (-82.0, 29.0), (-81.0, 28.0),
                                 (-81.5, 28.0), (-81.0, 28.0))[0] is False,
          "a geographic layer counts as reprojected once any output coordinate leaves the degree range  <-- pinned defect")
    ok, why = prove_reprojection(False, 0.3048006096, (0, 0), (1000, 0),
                                 (0, 0), (304.8, 0))
    check(ok and "scale ratio" in why, "a foot layer that became metres passes the scale proof")
    ok, why = prove_reprojection(False, 0.3048006096, (0, 0), (1000, 0),
                                 (0, 0), (1000, 0))
    check(not ok and "re-tagged" in why,
          "a foot layer whose coordinates did not change is caught as re-tagged  <-- pinned defect")
    ok, why = prove_reprojection(False, 1.0, (5, 5), (5, 5), (5, 5), (5, 5))
    check(not ok and "cannot be proved" in why,
          "a layer at one location cannot prove its scale and is refused")
    ok, why = prove_reprojection(True, 0.0174532925, (-82.0, 29.0), (-81.0, 28.0),
                                 (400000.0, 3200000.0), (500000.0, 3100000.0))
    check(ok, "a geographic layer that became metres passes")
    ok, why = prove_reprojection(True, 0.0174532925, (-82.0, 29.0), (-81.0, 28.0),
                                 (-82.0, 29.0), (-81.0, 28.0))
    check(not ok and "re-tagged" in why,
          "a geographic layer that still reads as degrees is caught as re-tagged  <-- pinned defect")
    ok, why = prove_reprojection(True, 0.0174532925, (500000.0, 3000000.0),
                                 (500100.0, 3000000.0), (500000.0, 3000000.0),
                                 (500100.0, 3000000.0))
    check(not ok, "a geographic layer whose coordinates did not change is caught even when they are large")
    ok, why = prove_reprojection(True, 0.0174532925, (-82.0, 29.0), (-81.0, 28.0),
                                 (-82.0, 29.5), (-81.0, 28.0))
    check(not ok, "a geographic layer moved by less than a degree is still degree-like")

    # ---- the reprojection, end to end, built by PROJ and proved back
    utm = [(400000.0, 3250000.0), (400800.0, 3250000.0), (400800.0, 3250600.0)]
    to_ft = pyproj.Transformer.from_crs(26917, 2237, always_xy=True)
    to_geo = pyproj.Transformer.from_crs(26917, 4269, always_xy=True)
    ft = [to_ft.transform(x, y) for x, y in utm]
    geo = [to_geo.transform(x, y) for x, y in utm]
    work = crs_facts(26917)
    line_ft = [Feat(0, "L", shapely.LineString(ft))]
    got, note = convert_layer(line_ft, f2237, work, "feet")
    back = list(got[0].geom.coords)
    check(all(abs(a[0] - b[0]) < 0.01 and abs(a[1] - b[1]) < 0.01
              for a, b in zip(back, utm)) and "scale ratio" in note,
          "a layer in US survey feet is reprojected to the metre CRS and proved by its scale")
    got, note = convert_layer([Feat(0, "L", shapely.LineString(geo))], f4269, work, "deg")
    back = list(got[0].geom.coords)
    check(all(abs(a[0] - b[0]) < 0.01 and abs(a[1] - b[1]) < 0.01
              for a, b in zip(back, utm)) and "degree-like" in note,
          "a layer in degrees is reprojected to the metre CRS and proved by its range")
    line_utm = [Feat(0, "L", shapely.LineString(utm))]
    got, note = convert_layer(line_utm, work, work, "same")
    check(got is line_utm and "not transformed" in note and "unchecked" not in note,
          "a layer already in the work CRS is passed through and says so")
    raises(lambda: convert_layer(line_ft, work, work, "feet-as-metres"), Refusal,
           "feet tagged as the work CRS fall outside its area of use and are refused  <-- pinned defect")
    deg_xy = [Feat(0, "L", shapely.LineString([(-82.0, 29.0), (-81.9, 29.1)]))]
    raises(lambda: convert_layer(deg_xy, work, work, "degrees-as-metres"), Refusal,
           "degrees tagged as the work CRS are refused instead of measured as metres  <-- pinned defect")
    got, note = convert_layer(line_utm, dict(work, aou=None), work, "same-noaou")
    check(got is line_utm and "unchecked" in note,
          "a layer in the work CRS with no area of use passes and notes the unchecked tag")
    raises(lambda: convert_layer([Feat(0, "L", shapely.LineString(utm))], f2237,
                                 work, "mistagged"), Refusal,
           "metre coordinates tagged as feet fall outside that CRS's area of use and are refused  <-- pinned defect")
    liar = dict(f2237, factors=(1.0, 1.0))
    raises(lambda: convert_layer(line_ft, liar, work, "liar"), Refusal,
           "a layer claimed to be metres that is really feet fails the scale proof end to end  <-- pinned defect")
    to_merc = pyproj.Transformer.from_crs(26917, 3857, always_xy=True)
    merc = [Feat(0, "L", shapely.LineString([to_merc.transform(x, y) for x, y in utm]))]
    why = ""
    try:
        convert_layer(merc, crs_facts(3857), work, "merc")
    except Refusal as exc:
        why = str(exc)
    check("scale ratio is 0.8" in why and "Web Mercator" in why and "Reproject it" in why,
          "correctly tagged Web Mercator is refused with a message that names its scale, not only 're-tagged'  <-- pinned defect")
    why = ""
    try:
        convert_layer(merc, crs_facts(3857), crs_facts(3857), "merc-same")
    except Refusal as exc:
        why = str(exc)
    check("is the work CRS, but" in why and "scale ratio is 1.1" in why and "Web Mercator" in why,
          "a layer tagged Web Mercator with Web Mercator as the work CRS is refused, not measured 1.32 times too large  <-- pinned defect")
    got, note = convert_layer(line_utm, work, work, "same-scale")
    check("scale ratio 0.9997" in note and "geodesic distance" in note,
          "a layer in the work CRS passes only after its scale is proved against the geodesic distance  <-- pinned defect")
    ok, why = prove_work_scale(0.0, 0.0)
    check(not ok and "cannot be proved" in why,
          "a layer at one location cannot prove the scale of the work CRS and is refused")
    ok, why = prove_work_scale(100.0, 0.0)
    check(not ok and "cannot be proved" in why,
          "a zero geodesic distance cannot prove the scale of the work CRS and is refused")
    ok, why = prove_work_scale(100.0, 100.0 * (1.0 + 2.0 * PROOF_TOL))
    check(not ok and "Name a projected CRS" in why,
          "a work-CRS scale ratio just outside the proof tolerance is refused")
    ok, why = prove_work_scale(100.0, 100.0 * (1.0 + 0.5 * PROOF_TOL))
    check(ok and "scale ratio" in why,
          "a work-CRS scale ratio just inside the proof tolerance passes")
    upright = [Feat(0, "L", shapely.LineString([(ft[0][0], ft[0][1]), (ft[0][0], ft[0][1] + 500.0),
                                                (ft[0][0], ft[0][1] + 1000.0)]))]
    got, note = convert_layer(upright, f2237, work, "upright")
    check("scale ratio" in note,
          "the scale proof uses the vertex farthest away in both axes, so a street that runs due north still passes  <-- pinned defect")
    unknown = dict(f2237, aou=None)
    got, note = convert_layer(line_ft, unknown, work, "noaou")
    check("unchecked" in note, "a CRS with no area of use still reprojects and notes the unchecked tag")
    line_geo = [Feat(0, "L", shapely.LineString(geo))]
    got, note = convert_layer(line_geo, f4269, work, "deg")
    check("scale ratio 0.9997" in note and "geodesic distance" in note,
          "a layer in degrees is proved by its scale against the geodesic distance  <-- pinned defect")
    why = ""
    try:
        convert_layer(line_geo, f4269, crs_facts(3857), "geo-to-merc")
    except Refusal as exc:
        why = str(exc)
    check("scale ratio is 1.1" in why and "Web Mercator" in why and "Name a projected CRS" in why,
          "degrees reprojected to Web Mercator are refused, where a 0.9 sq m overlap measured 1.19 sq m  <-- pinned defect")
    why = ""
    try:
        convert_layer(line_geo, f4269, crs_facts(26910), "geo-to-far-zone")
    except Refusal as exc:
        why = str(exc)
    check("area of use of the work CRS EPSG:26910" in why and "Name a work CRS that covers the data" in why
          and "tag is probably wrong" not in why,
          "a work CRS whose area of use does not hold the data is refused with its own message  <-- pinned defect")
    ok, why = prove_reprojection(True, 0.0174532925, (-82.0, 29.0), (-81.0, 28.0),
                                 (400000.0, 3200000.0), (500000.0, 3100000.0), 0.0)
    check(not ok and "cannot be proved" in why,
          "a geographic layer at one location cannot prove its scale and is refused")
    ok, why = prove_reprojection(True, 0.0174532925, (-82.0, 29.0), (-81.0, 28.0),
                                 (400000.0, 3200000.0), (500000.0, 3100000.0), 141421.0)
    check(ok, "a geographic layer whose output distance matches the geodesic distance passes")
    ok, why = prove_reprojection(True, 0.0174532925, (-82.0, 29.0), (-81.0, 28.0),
                                 (400000.0, 3200000.0), (500000.0, 3100000.0), 140000.0)
    check(not ok, "a geographic layer whose output distance is 1 percent off the geodesic distance is refused")

    # ---- reading GeoJSON
    def named(n):
        return {"crs": {"properties": {"name": n}}}

    check(declared_epsg({}) is None
          and declared_epsg(named("urn:ogc:def:crs:EPSG::2237")) == 2237
          and declared_epsg(named("EPSG:26917")) == 26917
          and declared_epsg(named("urn:ogc:def:crs:OGC:1.3:CRS84")) == 4326,
          "a legacy crs member is read as an EPSG code, and a file without one declares none")
    raises(lambda: declared_epsg({"crs": {"type": "name"}}), Refusal,
           "a crs member the tool cannot read is refused, not ignored")
    raises(lambda: declared_epsg({"crs": "EPSG:4326"}), Refusal,
           "a crs member that is not an object is refused")
    raises(lambda: declared_epsg(named("EPSG:2237x")), Refusal,
           "a crs name with text after the code is refused, not read as the code  <-- pinned defect")
    one = {"type": "Feature", "properties": {}, "geometry": None}
    check(raw_features(one, "p") == [one]
          and raw_features({"type": "FeatureCollection", "features": [one]}, "p") == [one],
          "a Feature and a FeatureCollection are both read")
    raises(lambda: raw_features({"type": "Point"}, "p"), ValueError,
           "a file that is not a Feature or FeatureCollection is unreadable")
    raises(lambda: raw_features([], "p"), ValueError,
           "a JSON list is not GeoJSON")
    raises(lambda: raw_features({"type": "FeatureCollection", "features": [1]}, "p"),
           ValueError, "a features list holding a non-object is unreadable")
    sq = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}
    fs, skipped = build_feats(
        [{"geometry": None}, {"geometry": {"type": "Polygon", "coordinates": []}},
         {"geometry": sq, "properties": {"KEY": "k"}}, {"geometry": sq, "properties": None},
         {"geometry": sq, "properties": {"KEY": ""}}], "polygon", "KEY", "t")
    check(skipped == 2 and [f.key for f in fs] == ["k", "#3", "#4"],
          "features with no geometry are counted, and a missing key becomes its position")
    raises(lambda: build_feats([{"geometry": {"type": "Foo"}}], "polygon", "KEY", "t"),
           ValueError, "a geometry shapely cannot build is unreadable")
    bow = {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}
    raises(lambda: build_feats([{"geometry": bow, "properties": {"KEY": "bow"}}],
                               "polygon", "KEY", "t"), Refusal,
           "a self-intersecting polygon refuses the run, because every area from it is wrong  <-- pinned defect")
    pt = {"type": "Point", "coordinates": [0, 0]}
    raises(lambda: build_feats([{"geometry": pt}], "polygon", "KEY", "t"), Refusal,
           "a point where a polygon is expected is refused")
    raises(lambda: build_feats([{"geometry": sq}], "line", "KEY", "t"), Refusal,
           "a polygon where a line is expected is refused")
    multi = {"type": "MultiLineString", "coordinates": [[[0, 0], [1, 0]], [[5, 5], [6, 5]]]}
    fs, _ = build_feats([{"geometry": multi, "properties": {"KEY": "m"}},
                         {"geometry": {"type": "LineString", "coordinates": [[0, 0, 9], [1, 0, 9]]}}],
                        "line", "KEY", "t")
    check([f.key for f in fs] == ["m", "m", "#1"] and len(fs[2].geom.coords[0]) == 2,
          "a MultiLineString becomes one street per part, and z values are dropped")

    # ---- reports and files
    sec = [("TITLE", ["  header"], [
        Finding("overlap", REPORTED, "overlap", "", "", 1.0, 1, 2, "d1"),
        Finding("overlap", REPORTED, "overlap", "k", "", 2.0, 3, 4, "d2"),
        Finding("overlap", INFO, "x", "k", "", None, 3, 4, "d3"),
        Finding("overlap", SUPPRESSED, "x", "k", "", 1.0, 3, 4, "d4")])]
    lines = describe(sec, ["note"], 1)
    check(lines[0] == "note" and "  REPORTED (2)" in lines
          and "    ... 1 more; --out lists them all" in lines
          and "    - at (1.00, 2.00): d1" in lines and "  INFO (1)" in lines
          and "  SUPPRESSED (1)" in lines,
          "the report lists a sample under each status and counts the rest")
    check(gate_exit(sec) == 1 and gate_exit([("T", [], [])]) == 0
          and gate_exit([("T", [], [sec[0][2][2], sec[0][2][3]])]) == 0,
          "only a reported finding fails the gate")
    csv_path = os.path.join(tmp, "f.csv")
    write_findings(csv_path, sec)
    with io.open(csv_path, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    check(tuple(rows[0]) == OUT_COLUMNS and len(rows) == 5 and rows[3][5] == ""
          and rows[4][1] == SUPPRESSED,
          "the findings CSV holds every finding, suppressed ones included, with its header")

    # ---- formula injection: a text cell never starts a spreadsheet formula
    clean_kw = {"check": "overlap", "status": REPORTED, "cls": "overlap",
                "key": "k", "other": "o", "detail": "d"}

    def row_of(**over):
        kw = dict(clean_kw, **over)
        return Finding(kw["check"], kw["status"], kw["cls"], kw["key"],
                       kw["other"], 1.0, 1, 2, kw["detail"]).row()

    clean_row = row_of()
    leaders = (("an equals sign", "="), ("a plus sign", "+"),
               ("a minus sign", "-"), ("an at sign", "@"),
               ("a tab", chr(9)), ("a carriage return", chr(13)))
    for field, column in (("check", "check"), ("status", "status"),
                          ("cls", "class"), ("key", "key"),
                          ("other", "other"), ("detail", "detail")):
        at_col = OUT_COLUMNS.index(column)
        for what, lead in leaders:
            got = row_of(**{field: lead + "x"})
            rest = [c for i, c in enumerate(got) if i != at_col]
            want = [c for i, c in enumerate(clean_row) if i != at_col]
            check(got[at_col] == "'" + lead + "x" and rest == want,
                  "the %s column gets one apostrophe before %s and nothing else changes  <-- pinned defect"
                  % (column, what))
    neg = Finding("gap", REPORTED, "gap", "k", "", -2.5, -10.0, -3.5, "d").row()
    check(neg[5:8] == ["-2.5000", "-10.000", "-3.500"]
          and [float(c) for c in neg[5:8]] == [-2.5, -10.0, -3.5],
          "negative value, x and y stay plain numbers with no apostrophe  <-- pinned defect")
    check(cell("") == "" and cell("ZONE-1") == "ZONE-1" and cell("a=b") == "a=b"
          and cell("'q'") == "'q'" and cell("#3") == "#3"
          and cell("=1+1") == "'=1+1",
          "a cell that does not start with a leader is written as it came, an empty one stays empty")
    hostile = [Finding("overlap", REPORTED, "overlap", chr(9) + "T", chr(13) + "C",
                       1.0, 1, 2, "=d")]
    hostile_csv = os.path.join(tmp, "hostile.csv")
    write_findings(hostile_csv, [("T", [], hostile)])
    with io.open(hostile_csv, "r", encoding="utf-8", newline="") as fh:
        hrows = list(csv.reader(fh))
    check(len(hrows) == 2 and hrows[1][3] == "'" + chr(9) + "T"
          and hrows[1][4] == "'" + chr(13) + "C" and hrows[1][8] == "'=d"
          and hrows[1][5:8] == ["1.0000", "1.000", "2.000"],
          "write_findings puts the apostrophe in the file for a tab and a CR key, and the file reads back as one row")

    # ---- the CLI, end to end
    BX, BY = 400000.0, 3250000.0

    def sqp(x, y, w=10.0, hgt=10.0):
        return box(BX + x, BY + y, BX + x + w, BY + y + hgt)

    areas = write_json("areas.geojson", [
        pfeat("N", sqp(0, 0)), pfeat("S", sqp(5, 0)), pfeat("Q", sqp(300, 0)),
        pfeat("R", sqp(309.95, 0))])
    clean = write_json("clean.geojson", [pfeat("N", sqp(0, 0)), pfeat("S", sqp(10, 0))])
    base = ["--work-epsg", "26917"]
    code, out, err = run(["--polygons", areas, "--polygons-epsg", "26917"] + base)
    check(code == 1 and "OVERLAPS" in out and "'N' overlaps 'S' by 50.0000 sq m" in out
          and "area threshold: a case must exceed 1 sq m" in out
          and "1 over the threshold, 1 suppressed" in out and "nearest case above" in out
          and "measured again on the uncut polygons" in out and err == "",
          "polygons that overlap exit 1 and the report names the threshold, the suppressed case and the nearest above")
    check("GAPS" in out and "0 uncovered hole(s)" in out,
          "the same run reports its gap check even when it finds no hole")
    seam = write_json("seam.geojson", [pfeat("WEST", sqp(0, 0, 100, 100)), pfeat("EAST", sqp(105, 0, 100, 100))])
    code, out, err = run(["--polygons", seam, "--polygons-epsg", "26917"] + base)
    check(code == 0 and "0 uncovered hole(s)" in out and "does not rule it out" in out and err == "",
          "a 5 m seam that runs to the outer edge exits 0 and the report says the strip was not checked  <-- pinned defect")
    shut = write_json("shut.geojson", [pfeat("WEST", sqp(0, 0, 100, 100)), pfeat("EAST", sqp(105, 0, 100, 100)),
                                       pfeat("TOP", sqp(0, 100, 205, 10)), pfeat("BOTTOM", sqp(0, -10, 205, 10))])
    code, out, err = run(["--polygons", shut, "--polygons-epsg", "26917"] + base)
    check(code == 1 and "1 uncovered hole(s)" in out and "500.0000 sq m" in out,
          "the same seam closed at both ends is a hole and fails the gate")
    code, out, err = run(["--polygons", clean, "--polygons-epsg", "26917"] + base)
    check(code == 0 and "0 over the threshold, 0 suppressed" in out,
          "clean polygons exit 0")
    outcsv = os.path.join(tmp, "out.csv")
    code, out, err = run(["--polygons", areas, "--polygons-epsg", "26917",
                          "--out", outcsv] + base)
    check(code == 1 and not os.path.exists(outcsv) and "Check only" in out,
          "without --apply the findings file is not written")
    code, out, err = run(["--polygons", areas, "--polygons-epsg", "26917",
                          "--out", outcsv, "--apply"] + base)
    with io.open(outcsv, "r", encoding="utf-8") as fh:
        body = fh.read()
    check(code == 1 and "wrote " in out and "suppressed,overlap,Q,R" in body,
          "with --apply the findings file is written and keeps the suppressed case")
    hot_areas = write_json("hot.geojson", [
        pfeat("=SUM(1+1)", sqp(0, 0)), pfeat(chr(9) + "B", sqp(5, 0)),
        pfeat("+Z", sqp(100, 0)), pfeat("@Y", sqp(105, 0)),
        pfeat("-X", sqp(200, 0)), pfeat(chr(13) + "W", sqp(205, 0))])
    hot_csv = os.path.join(tmp, "hot.csv")
    code, out, err = run(["--polygons", hot_areas, "--polygons-epsg", "26917",
                          "--out", hot_csv, "--apply"] + base)
    with io.open(hot_csv, "r", encoding="utf-8", newline="") as fh:
        hot = list(csv.reader(fh))[1:]
    pairs = sorted(sorted([r[3], r[4]]) for r in hot if r[0] == "overlap")
    check(code == 1 and pairs == sorted([
        sorted(["'=SUM(1+1)", "'" + chr(9) + "B"]), sorted(["'+Z", "'@Y"]),
        sorted(["'-X", "'" + chr(13) + "W"])]),
          "a real run over keys that start with = + - @ tab and CR writes each with one apostrophe  <-- pinned defect")
    check(all(r[i][:1] not in FORMULA_LEADERS for r in hot for i in (0, 1, 2, 3, 4, 8))
          and all(isinstance(float(r[j]), float) for r in hot for j in (6, 7)),
          "no text cell of the findings file starts with a formula leader and x and y stay numbers")
    check("overlaps" in out and "'=SUM(1+1)'" in out,
          "the terminal report still shows the key as it came, only the file is guarded")
    readme_hot = write_json("readme_hot.geojson", [
        pfeat("=SUM(1+1)", sqp(0, 0)), pfeat("-ZONE-2", sqp(5, 0))])
    readme_csv = os.path.join(tmp, "readme_hot.csv")
    code, out, err = run(["--polygons", readme_hot, "--polygons-epsg", "26917",
                          "--out", readme_csv, "--apply"] + base)
    with io.open(readme_csv, "r", encoding="utf-8", newline="") as fh:
        readme_lines = fh.read().split("\r\n")
    check(code == 1 and readme_lines[1] == "overlap,reported,overlap,'=SUM(1+1),'-ZONE-2,50.0000,"
          "400007.500,3250005.000,'=SUM(1+1)' overlaps '-ZONE-2' by 50.0000 sq m",
          "the README hot.csv example is exactly what two 10 m squares offset 5 m, keyed =SUM(1+1) and -ZONE-2, produce")
    hot_streets = write_json("hot_streets.geojson", [
        lfeat("-A", [[BX, BY], [BX + 100, BY]]),
        lfeat("@B", [[BX + 50, BY + 5], [BX + 50, BY + 60]])])
    hot_scsv = os.path.join(tmp, "hot_streets.csv")
    code, out, err = run(["--streets", hot_streets, "--streets-epsg", "26917",
                          "--out", hot_scsv, "--apply"] + base)
    with io.open(hot_scsv, "r", encoding="utf-8", newline="") as fh:
        srows = list(csv.reader(fh))[1:]
    check(code == 1 and any(r[2] == "fallout" and r[3] == "'@B" and r[4] == "'-A"
                            for r in srows)
          and "end stops 5.00 m from '-A'" in out,
          "a street key that starts with a minus sign or an at sign is guarded in the snapgap rows  <-- pinned defect")
    blocked = os.path.join(tmp, "blocked.csv")
    code, out, err = run(["--polygons", areas, "--polygons-epsg", "26917",
                          "--out", blocked, "--appl"] + base)
    check(code == 64 and not os.path.exists(blocked),
          "a prefix of the write flag is refused and nothing is written  <-- pinned defect")
    for prefix in ("--ap", "--app", "--apply-"):
        code, out, err = run(["--polygons", areas, "--polygons-epsg", "26917",
                              "--out", blocked, prefix] + base)
        check(code == 64 and not os.path.exists(blocked),
              "the prefix %s is refused by the parser  <-- pinned defect" % prefix)
    code, out, err = run(["--poly", areas])
    check(code == 64 and "unrecognized arguments" in err,
          "a prefix of any other flag is refused as well  <-- pinned defect")
    bad_dir = os.path.join(tmp, "nodir", "out.csv")
    code, out, err = run(["--polygons", areas, "--polygons-epsg", "26917",
                          "--out", bad_dir, "--apply"] + base)
    check(code == 2 and "could not write" in err,
          "a findings file that cannot be written exits 2, not 1")
    code, out, err = run(["--polygons", os.path.join(tmp, "absent.geojson"),
                          "--polygons-epsg", "26917"] + base)
    check(code == 2 and out == "" and "absent.geojson" in err,
          "a missing input file exits 2 and reports nothing")
    junk = os.path.join(tmp, "junk.geojson")
    with io.open(junk, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    code, out, err = run(["--polygons", junk, "--polygons-epsg", "26917"] + base)
    check(code == 2 and out == "" and "junk.geojson: Expecting" in err,
          "a file that is not JSON exits 2 and the message names the file  <-- pinned defect")
    emptyfc = write_json("empty.geojson", [])
    code, out, err = run(["--polygons", emptyfc, "--polygons-epsg", "26917"] + base)
    check(code == 4 and out == "" and "nothing was compared" in err,
          "a run that compared nothing exits 4 and does not call itself clean  <-- pinned defect")
    nogeom = write_json("nogeom.geojson", [{"type": "Feature", "properties": {}, "geometry": None}])
    code, out, err = run(["--polygons", nogeom, "--polygons-epsg", "26917"] + base)
    check(code == 4, "a layer whose every feature has no geometry also compares nothing")
    mism = write_json("mism.geojson", [pfeat("N", sqp(0, 0))],
                      crs={"type": "name", "properties": {"name": "EPSG:2237"}})
    code, out, err = run(["--polygons", mism, "--polygons-epsg", "26917"] + base)
    check(code == 3 and out == "" and "declares EPSG:2237" in err,
          "a file whose crs member disagrees with the flag is refused  <-- pinned defect")
    code, out, err = run(["--polygons", clean, "--polygons-epsg", "2237"] + base)
    check(code == 3 and out == "" and "area of use" in err,
          "an input tagged with the wrong EPSG is refused before anything is measured  <-- pinned defect")
    ll = write_json("ll.geojson", [pfeat("A", box(-82.0, 29.0, -81.999, 29.001)),
                                   pfeat("B", box(-81.9995, 29.0, -81.998, 29.001))])
    code, out, err = run(["--polygons", ll, "--polygons-epsg", "4326"] + base)
    check(code == 1 and "scale ratio 0.9997" in out and "geodesic" in out,
          "degree polygons are measured in the work CRS once its scale is proved")
    code, out, err = run(["--polygons", ll, "--polygons-epsg", "4326", "--work-epsg", "3857"])
    check(code == 3 and out == "" and "Web Mercator" in err,
          "degree polygons are refused under Web Mercator and nothing is printed  <-- pinned defect")
    merc_sq = [pfeat("A", shapely.ops.transform(to_merc.transform, box(400000.0, 3250000.0, 400010.0, 3250010.0))),
               pfeat("B", shapely.ops.transform(to_merc.transform, box(400009.0, 3250000.0, 400020.0, 3250010.0)))]
    merc_same = write_json("merc_same.geojson", merc_sq)
    code, out, err = run(["--polygons", merc_same, "--polygons-epsg", "3857", "--work-epsg", "3857"])
    check(code == 3 and out == "" and "Web Mercator" in err and "is the work CRS, but" in err,
          "polygons tagged 3857 under work CRS 3857 are refused and nothing is printed, where a 0.76 sq m overlap read 1.0 and failed the gate  <-- pinned defect")
    code, out, err = run(["--polygons", clean, "--polygons-epsg", "999999"] + base)
    check(code == 3 and "not a code PROJ knows" in err, "an unknown EPSG for an input is refused")
    code, out, err = run(["--polygons", clean, "--polygons-epsg", "26917",
                          "--work-epsg", "4326"])
    check(code == 3 and "not a projected CRS" in err,
          "a geographic work CRS is refused, because its distances are degrees  <-- pinned defect")
    code, out, err = run(["--polygons", clean, "--polygons-epsg", "26917",
                          "--work-epsg", "2237"])
    check(code == 3 and "measures in US survey foot" in err,
          "a work CRS in feet is refused, because the thresholds are metres")
    badpoly = write_json("bow.geojson", [pfeat("bow", shapely.Polygon(
        [(BX, BY), (BX + 1, BY + 1), (BX + 1, BY), (BX, BY + 1)]))])
    code, out, err = run(["--polygons", badpoly, "--polygons-epsg", "26917"] + base)
    check(code == 3 and "is not valid" in err, "an invalid polygon exits 3 through the CLI")

    # streets, including a layer in feet
    def sline(key, pts):
        return lfeat(key, [[BX + x, BY + y] for x, y in pts])

    roads = [sline("A", [(0, 0), (100, 0)]), sline("B", [(50, 5), (50, 60)]),
             sline("C", [(500, 505), (500, 900)]), sline("D", [(300, 500), (700, 500)])]
    streets = write_json("streets.geojson", roads)
    code, out, err = run(["--streets", streets, "--streets-epsg", "26917"] + base)
    check(code == 1 and "SNAPGAP" in out and "end stops 5.00 m from 'A'" in out
          and "no boundary layer" in out and "<-- densest" not in out,
          "streets with an end 5 m short exit 1 and the report says no boundary layer was used")
    ft_streets = write_json("streets_ft.geojson", [
        lfeat(r["properties"]["SEGMENT_ID"],
              [list(to_ft.transform(x, y)) for x, y in r["geometry"]["coordinates"]])
        for r in roads])
    code, out2, err = run(["--streets", ft_streets, "--streets-epsg", "2237"] + base)
    check(code == 1 and "EPSG:2237 -> EPSG:26917" in out2 and "end stops 5.00 m from 'A'" in out2,
          "the same streets given in feet find the same fallout after reprojection")
    stub_set = [sline("T", [(0, 0), (1000, 0)])]
    for n, d in enumerate((6.5, 7.0, 7.2, 7.6)):
        stub_set.append(sline("S%d" % n, [(100 * (n + 1), d), (100 * (n + 1), 50)]))
    code, out, err = run(["--streets", write_json("stubs.geojson", stub_set),
                          "--streets-epsg", "26917"] + base)
    check(code == 1 and "6.0 -    8.0 m    4  ####  <-- densest" in out,
          "four undershoots of 6.5 to 7.6 m show as one densest histogram bin in the report")
    lone_f = write_json("lone.geojson", [sline("L", [(0, 0), (100, 0)])])
    code, out, err = run(["--streets", lone_f, "--streets-epsg", "26917"] + base)
    check(code == 0 and "INFO (2)" in out and "distance from a dangling end" not in out,
          "a lone street lists two dead ends as information and exits 0")
    county_f = write_json("county.geojson", [pfeat("C", sqp(-5, -5, 1500, 1500))])
    code, out, err = run(["--streets", streets, "--streets-epsg", "26917",
                          "--boundary", county_f, "--boundary-epsg", "26917"] + base)
    check(code == 1 and "SUPPRESSED" in out and "no boundary layer" not in out,
          "a trusted boundary layer is used and its suppressions are listed")
    flood = write_json("edge.geojson", [sline("E%d" % i, [(10 * i, 0), (10 * i + 5, 0)])
                                        for i in range(20)])
    code, unguarded, err = run(["--streets", flood, "--streets-epsg", "26917"] + base)
    code, out, err = run(["--streets", flood, "--streets-epsg", "26917",
                          "--boundary", os.path.join(tmp, "gone.geojson"),
                          "--boundary-epsg", "26917"] + base)
    check(code == 2 and out == "" and "gone.geojson" in err and "dangling" in unguarded,
          "a boundary layer that cannot be read exits 2 and prints no findings, where the unguarded run printed them  <-- pinned defect")
    wrong = write_json("wrong.geojson", [pfeat("C", box(BX + 300000, BY, BX + 301000, BY + 1000))])
    code, out, err = run(["--streets", flood, "--streets-epsg", "26917",
                          "--boundary", wrong, "--boundary-epsg", "26917"] + base)
    check(code == 3 and out == "" and "cannot be trusted" in err and "does not overlap" in err,
          "a boundary layer in the wrong place is refused with no findings printed  <-- pinned defect")
    tiny = write_json("tiny.geojson", [pfeat("C", sqp(0, -5, 25, 10))])
    code, out, err = run(["--streets", flood, "--streets-epsg", "26917",
                          "--boundary", tiny, "--boundary-epsg", "26917"] + base)
    check(code == 3 and out == "" and "street ends lie inside" in err,
          "a boundary layer that holds a few street ends is refused with no findings printed")
    point_b = write_json("pointb.geojson", [{"type": "Feature", "properties": {},
                                              "geometry": {"type": "Point", "coordinates": [BX, BY]}}])
    code, out, err = run(["--streets", flood, "--streets-epsg", "26917",
                          "--boundary", point_b, "--boundary-epsg", "26917"] + base)
    check(code == 3 and out == "" and "not a polygon" in err,
          "a boundary layer that is not polygons is refused")
    code, out, err = run(["--streets", flood, "--streets-epsg", "26917",
                          "--boundary", emptyfc, "--boundary-epsg", "26917"] + base)
    check(code == 3 and out == "" and "has no polygons" in err,
          "a boundary layer with no polygons is refused")
    code, out, err = run(["--streets", emptyfc, "--streets-epsg", "26917",
                          "--boundary", wrong, "--boundary-epsg", "26917"] + base)
    check(code == 4, "an empty street layer with a boundary layer compares nothing")
    both = run(["--polygons", clean, "--polygons-epsg", "26917", "--streets", streets,
                "--streets-epsg", "26917"] + base)
    check(both[0] == 1 and "OVERLAPS" in both[1] and "SNAPGAP" in both[1],
          "polygons and streets can be checked in one run")
    code, out, err = run(["--polygons", clean, "--polygons-epsg", "26917",
                          "--streets", emptyfc, "--streets-epsg", "26917"] + base)
    check(code == 4 and "OVERLAPS" in out and "SNAPGAP" not in out
          and "--streets held no features" in err,
          "an empty street layer beside good polygons exits 4 and names the layer, not a clean 0  <-- pinned defect")
    code, out, err = run(["--polygons", nogeom, "--polygons-epsg", "26917",
                          "--streets", streets, "--streets-epsg", "26917"] + base)
    check(code == 4 and "SNAPGAP" in out and "OVERLAPS" not in out
          and "--polygons held no features" in err,
          "a polygon layer with no geometry beside good streets exits 4 and names the layer  <-- pinned defect")
    code, out, err = run(["--polygons", nogeom, "--polygons-epsg", "26917",
                          "--streets", emptyfc, "--streets-epsg", "26917"] + base)
    check(code == 4 and out == "" and "nothing was compared" in err,
          "two empty layers still exit 4 with the nothing-compared message")
    deg_poly = write_json("deg_poly.geojson", [
        pfeat("A", box(-82.0, 29.0, -81.9, 29.1)), pfeat("B", box(-81.95, 29.0, -81.85, 29.1))])
    code, out, err = run(["--polygons", deg_poly, "--polygons-epsg", "26917"] + base)
    check(code == 3 and out == "" and "area of use" in err,
          "degrees tagged as the metre work CRS are refused, not measured as a 0.005 sq m overlap  <-- pinned defect")
    nan_st = write_json("nan_st.geojson", [lfeat("N1", [[BX, BY], [BX + 100, BY]]),
                                           lfeat("N2", [[BX + 106, BY], [float("nan"), BY + 50]])])
    code, out, err = run(["--streets", nan_st, "--streets-epsg", "26917"] + base)
    check(code == 3 and out == "" and "nan or infinite" in err,
          "a street with a nan coordinate is refused, not listed as a clean dead end with exit 0  <-- pinned defect")
    inf_st = write_json("inf_st.geojson", [lfeat("I1", [[BX, BY], [float("inf"), BY + 50]])])
    code, out, err = run(["--streets", inf_st, "--streets-epsg", "26917"] + base)
    check(code == 3 and out == "" and "nan or infinite" in err,
          "a street with an infinite coordinate is refused by the same check")
    precious = os.path.join(tmp, "precious.txt")
    with io.open(precious, "w", encoding="utf-8") as fh:
        fh.write(u"keep me")
    code, out, err = run(["--polygons", areas, "--polygons-epsg", "26917",
                          "--out", precious] + base)
    with io.open(precious, "r", encoding="utf-8") as fh:
        kept = fh.read()
    code2, out2, err2 = run(["--polygons", areas, "--polygons-epsg", "26917",
                             "--out", precious, "--apply"] + base)
    with io.open(precious, "r", encoding="utf-8") as fh:
        replaced = fh.read()
    check(kept == "keep me" and replaced.startswith("check,status,class,key")
          and code2 == 1,
          "--out is refused only when it is an input; with --apply an existing file is overwritten  <-- pinned defect")

    sur = write_json("sur.geojson", [pfeat(u"\ud800x", sqp(0, 0)), pfeat("S", sqp(5, 0))])
    sur_csv = os.path.join(tmp, "sur.csv")
    with io.open(sur_csv, "w", encoding="utf-8") as fh:
        fh.write(u"keep me")
    code, out, err = run(["--polygons", sur, "--polygons-epsg", "26917",
                          "--out", sur_csv, "--apply"] + base)
    with io.open(sur_csv, "r", encoding="utf-8") as fh:
        kept = fh.read()
    check(code == 2 and "could not write" in err and kept == "keep me",
          "a key with a lone surrogate exits 2 and leaves the old findings file untouched, not exit 70 and a partial file  <-- pinned defect")

    # ---- the report: how many findings are listed, and which bin is densest
    three = [Finding("overlap", REPORTED, "overlap", "k%d" % n, "", 1.0, 0.0, 0.0, "d")
             for n in range(3)]
    shown = describe([("T", [], three)], [], 2)
    check(sum(1 for t in shown if t.startswith("    k")) == 2
          and shown[-1] == "    ... 1 more; --out lists them all",
          "--sample caps the findings listed and says how many more there are  <-- pinned defect")
    shown = describe([("T", [], three[:2])], [], 2)
    check(sum(1 for t in shown if t.startswith("    k")) == 2
          and not any("more" in t for t in shown),
          "a group exactly the size of --sample lists everything and mentions nothing more")

    def stubs_run(offsets, extra=()):
        feats = [sline("T", [(0, 0), (1000, 0)])]
        for n, d in enumerate(offsets):
            feats.append(sline("S%d" % n, [(100 * (n + 1), d), (100 * (n + 1), 80)]))
        return run(["--streets", write_json("stubs3.geojson", feats),
                    "--streets-epsg", "26917"] + base + list(extra))

    code, out, err = stubs_run((6.5, 7.0, 7.2))
    check("6.0 -    8.0 m    3  ###  <-- densest" in out,
          "a bin holding exactly three ends is marked densest  <-- pinned defect")
    code, out, err = stubs_run((6.5, 7.0))
    check("6.0 -    8.0 m    2  ##" in out and "densest" not in out,
          "a bin holding two ends is not marked")
    code, out, err = stubs_run((6.5, 7.0, 7.2, 10.5, 10.8, 11.0), ("--snap-tol", "12"))
    marked = [t for t in out.splitlines() if "densest" in t]
    check(len(marked) == 1 and "6.0 -" in marked[0],
          "two equally dense bins mark the nearer one  <-- pinned defect")
    code, out, err = stubs_run((6.5, 7.0, 7.2), ("--sample", "0"))
    check(code == 1 and "REPORTED (3)" in out and "... 3 more" in out and "end stops" not in out,
          "--sample 0 is accepted and lists no finding rows  <-- pinned defect")

    # ---- a boundary layer is vetted with the snap tolerance
    near_edge = write_json("near_edge.geojson", [
        sline("S1", [(102, 0), (104, 5)]), sline("S2", [(101, 10), (103, 12)]),
        sline("S3", [(50, 0), (60, 0)])])
    bnd = write_json("bnd.geojson", [pfeat("C", sqp(0, -50, 100, 100))])
    code, out, err = run(["--streets", near_edge, "--streets-epsg", "26917",
                          "--boundary", bnd, "--boundary-epsg", "26917"] + base)
    check(code == 0 and "SUPPRESSED (4)" in out and err == "",
          "street ends a few metres outside the boundary count as inside it when the boundary is vetted  <-- pinned defect")

    # ---- usage errors
    pe = ["--polygons", clean, "--polygons-epsg", "26917"]
    cases = [
        ([], "name --polygons or --streets"),
        (["--polygons", clean] + base, "needs --polygons-epsg"),
        (["--polygons-epsg", "26917", "--streets", streets, "--streets-epsg", "26917"] + base,
         "needs --polygons"),
        (pe, "--work-epsg is required"),
        (pe + ["--boundary", clean, "--boundary-epsg", "26917"] + base, "needs --streets"),
        (pe + ["--sample", "-1"] + base, "--sample"),
        (pe + ["--bin", "0"] + base, "--bin"),
        (pe + ["--gap-tol", "-1"] + base, "negative"),
        (pe + ["--min-boundary-cover", "2"] + base, "share"),
        (pe + ["--node-tol", "0"] + base, "--node-tol"),
        (pe + ["--snap-tol", "0.01"] + base, "--node-tol"),
        (pe + ["--isolated-tol", "1"] + base, "--isolated-tol"),
        (pe + ["--apply"] + base, "--apply needs --out"),
        (pe + ["--out", clean] + base, "also an input"),
    ]
    for argv, needle in cases:
        code, out, err = run(argv)
        check(code == 64 and needle in err and out == "",
              "usage error %r exits 64 and says so" % needle)
    code, out, err = run(["--help"])
    check(code == 0 and "--self-test" in out, "--help exits 0")

    def accepted(extra):
        return _usage_problem(_parse(pe + base + extra)) is None

    check(accepted([]) and accepted(["--sample", "0"]) and accepted(["--min-boundary-cover", "1"])
          and accepted(["--min-boundary-cover", "0"]) and accepted(["--min-gap-width", "0"]),
          "the edges of each allowed range are accepted: --sample 0, a share of 0 or 1, a width gate of 0  <-- pinned defect")
    check(not accepted(["--min-gap-width", "-1"]) and not accepted(["--snap-tol", "0.05"])
          and not accepted(["--min-boundary-cover", "1.01"]) and not accepted(["--bin", "-1"]),
          "a negative width gate, a snap tolerance equal to the node tolerance and a share over 1 are refused  <-- pinned defect")
    nonfinite = [["--overlap-tol", "nan"], ["--gap-tol", "nan"], ["--min-gap-width", "nan"],
                 ["--overlap-tol", "inf"], ["--snap-tol", "inf"], ["--isolated-tol", "nan"],
                 ["--node-tol", "nan"], ["--bin", "inf"], ["--min-boundary-cover", "nan"]]
    check(not any(accepted(extra) for extra in nonfinite) and accepted(["--overlap-tol", "0"]),
          "a threshold of nan or inf is refused at parse time and 0 still passes  <-- pinned defect")
    code, out, err = run(["--polygons", areas, "--polygons-epsg", "26917",
                          "--overlap-tol", "nan"] + base)
    check(code == 64 and out == "" and "finite" in err,
          "--overlap-tol nan exits 64 and prints no report, not a clean 0 with a real overlap hidden  <-- pinned defect")
    dflt = _parse(pe + base)
    check((dflt.overlap_tol, dflt.gap_tol, dflt.min_gap_width, dflt.snap_tol, dflt.node_tol,
           dflt.min_boundary_cover, dflt.bin, dflt.sample, dflt.key_field,
           dflt.street_key_field)
          == (1.0, 1.0, 0.15, 9.0, 0.05, 0.5, 2.0, 10, "KEY", "SEGMENT_ID")
          and (CLIP_EPS, MIN_CLUSTER, ENCLAVE_SUSPECT_MULT, BORDER_PAD, PROOF_TOL, AOU_MARGIN_DEG)
          == (1e-6, 3, 20.0, 0.01, 0.005, 0.1),
          "the default thresholds are the ones the README documents  <-- pinned defect")

    # ---- the tool opens no sockets and does not crash quietly
    real_connect = socket.socket.connect
    calls = []

    def boom(self, *a, **k):
        calls.append(a)
        raise OSError("no network in this test")

    socket.socket.connect = boom
    try:
        code, _, _ = run(["--polygons", areas, "--polygons-epsg", "26917"] + base)
        during_run = len(calls)
        probe = socket.socket()
        try:
            probe.connect(("127.0.0.1", 9))
        except OSError:
            pass
        probe.close()
    finally:
        socket.socket.connect = real_connect
    check(code == 1 and during_run == 0 and len(calls) == 1,
          "a full run opens no socket, and the guard would have caught one")
    real_run = globals()["_run"]

    def crash(argv):
        raise RuntimeError("fault")

    globals()["_run"] = crash
    try:
        code, out, err = run(["--self-test"])
    finally:
        globals()["_run"] = real_run
    check(code == 70 and "not a finding about the data" in err,
          "an unexpected exception exits 70, not the 1 that means a gate failed  <-- pinned defect")
    real_load = globals()["load_libs"]

    def no_libs():
        raise MissingLibs("shapely is absent")

    globals()["load_libs"] = no_libs
    try:
        code, out, err = run(["--polygons", clean])
    finally:
        globals()["load_libs"] = real_load
    check(code == 69 and "shapely is absent" in err, "missing libraries exit 69")

    def no_import(name):
        raise ImportError(name)

    raises(lambda: load_libs(importer=no_import), MissingLibs,
           "an import failure is reported as missing libraries")

    class Old(object):
        __version__ = "1.8.5"

    raises(lambda: load_libs(importer=lambda name: Old()), MissingLibs,
           "shapely 1 is refused, because its API differs  <-- pinned defect")

    class Odd(object):
        __version__ = "dev"

    raises(lambda: load_libs(importer=lambda name: Odd()), MissingLibs,
           "a version string with no number is treated as too old")

    # ---- a key the console cannot encode
    ascii_out = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    ascii_err = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    accent = write_json("accent.geojson", [pfeat(u"Z" + chr(246), sqp(0, 0)), pfeat(u"Y", sqp(5, 0))])
    with contextlib.redirect_stdout(ascii_out), contextlib.redirect_stderr(ascii_err):
        code = main(["--polygons", accent, "--polygons-epsg", "26917"] + base)
    ascii_out.flush()
    shown = ascii_out.buffer.getvalue().decode("ascii")
    check(code == 1 and "'Z?' overlaps 'Y'" in shown,
          "a key the console cannot encode prints as ? and the verdict still arrives  <-- pinned defect")

    # ---- the module and the harness
    spec = importlib.util.spec_from_file_location("ng911topo_import", os.path.abspath(__file__))
    mod = importlib.util.module_from_spec(spec)
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        spec.loader.exec_module(mod)
    check(sink.getvalue() == "" and hasattr(mod, "find_overlaps"),
          "importing the module prints nothing and exposes the core")
    inner = _Harness()
    quiet = io.StringIO()
    with contextlib.redirect_stdout(quiet):
        inner.check(False, "a failing check")
        inner.raises(lambda: None, ValueError, "no error")
        inner.raises(lambda: 1 // 0, ValueError, "wrong error")
        inner.raises(lambda: int("x"), ValueError, "the right error")
        status = inner.footer()
    meta(status == 1 and len(inner.failed) == 3 and inner.passed == 1,
         "check() and raises() really do record a failure  <-- pinned defect")
    meta("4 assertions, 3 failed" in quiet.getvalue() and "FAILED: no error" in quiet.getvalue(),
         "the footer reports failures by count and by name  <-- pinned defect")
    probe = _Harness()
    with contextlib.redirect_stdout(io.StringIO()):
        probe.meta(False, "broken")
        probe.meta(True, "fine")
    meta(probe.failed == ["broken"] and probe.passed == 1,
         "the harness check that bypasses check() records a failure and a pass  <-- pinned defect")
    quiet = io.StringIO()
    with contextlib.redirect_stdout(quiet):
        status = _Harness().footer()
    check(status == 0 and "0 assertions, 0 failed" in quiet.getvalue(),
          "an empty run reports zero failures and exits 0")

    status = h.footer()
    return 1 if h.failed else status



# --------------------------------------------------------------------- cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="ng911topo.py",
        description="Find response-area polygons that overlap, ground no "
                    "polygon covers, and street ends that stop short.",
        epilog="Every input names its EPSG code. Nothing is written without "
               "--apply.",
        allow_abbrev=False,
    )
    ap.add_argument("--polygons", help="response-area polygons as GeoJSON")
    ap.add_argument("--polygons-epsg", dest="polygons_epsg", type=int,
                    help="the EPSG code of --polygons")
    ap.add_argument("--streets", help="street centerlines as GeoJSON")
    ap.add_argument("--streets-epsg", dest="streets_epsg", type=int,
                    help="the EPSG code of --streets")
    ap.add_argument("--boundary",
                    help="polygon GeoJSON outside which street ends are "
                         "expected. Refused when it cannot be trusted.")
    ap.add_argument("--boundary-epsg", dest="boundary_epsg", type=int,
                    help="the EPSG code of --boundary")
    ap.add_argument("--work-epsg", dest="work_epsg", type=int,
                    help="the projected metre CRS every layer is brought to")
    ap.add_argument("--key-field", dest="key_field", default="KEY",
                    help="the polygon property that names a response area "
                         "(default KEY)")
    ap.add_argument("--street-key-field", dest="street_key_field",
                    default="SEGMENT_ID",
                    help="the street property that names a segment "
                         "(default SEGMENT_ID)")
    ap.add_argument("--overlap-tol", dest="overlap_tol", type=float,
                    default=DEFAULT_OVERLAP_TOL,
                    help="sq m an overlap must exceed (default %g)"
                         % DEFAULT_OVERLAP_TOL)
    ap.add_argument("--gap-tol", dest="gap_tol", type=float,
                    default=DEFAULT_GAP_TOL,
                    help="sq m an uncovered hole must exceed (default %g)"
                         % DEFAULT_GAP_TOL)
    ap.add_argument("--min-gap-width", dest="min_gap_width", type=float,
                    default=DEFAULT_MIN_GAP_WIDTH,
                    help="metres of mean width a hole must exceed "
                         "(default %g)" % DEFAULT_MIN_GAP_WIDTH)
    ap.add_argument("--snap-tol", dest="snap_tol", type=float,
                    default=DEFAULT_SNAP_TOL,
                    help="metres within which a street end should have "
                         "snapped (default %g)" % DEFAULT_SNAP_TOL)
    ap.add_argument("--isolated-tol", dest="isolated_tol", type=float,
                    help="metres searched before a dead end is called "
                         "isolated (default: the snap tolerance)")
    ap.add_argument("--node-tol", dest="node_tol", type=float,
                    default=DEFAULT_NODE_TOL,
                    help="metres within which two street ends are one node "
                         "(default %g)" % DEFAULT_NODE_TOL)
    ap.add_argument("--bin", type=float, default=DEFAULT_BIN,
                    help="histogram bin width in metres (default %g)"
                         % DEFAULT_BIN)
    ap.add_argument("--min-boundary-cover", dest="min_boundary_cover",
                    type=float, default=DEFAULT_MIN_BOUNDARY_COVER,
                    help="share of street ends that must lie inside "
                         "--boundary (default %g)"
                         % DEFAULT_MIN_BOUNDARY_COVER)
    ap.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                    help="findings listed under each heading (default %d)"
                         % DEFAULT_SAMPLE)
    ap.add_argument("--out", help="path for the findings CSV")
    ap.add_argument("--apply", action="store_true",
                    help="write --out. Without this nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def _usage_problem(args):
    """The first thing wrong with the arguments, or None."""
    layers = (("polygons", args.polygons, args.polygons_epsg),
              ("streets", args.streets, args.streets_epsg),
              ("boundary", args.boundary, args.boundary_epsg))
    if not args.polygons and not args.streets:
        return ("name --polygons or --streets. Use --self-test to verify the "
                "tool without them.")
    for name, path, epsg in layers:
        if path and epsg is None:
            return ("--%s needs --%s-epsg. The tool never assumes a "
                    "coordinate system." % (name, name))
        if epsg is not None and not path:
            return "--%s-epsg needs --%s" % (name, name)
    if args.work_epsg is None:
        return "--work-epsg is required: the projected metre CRS to measure in"
    if args.boundary and not args.streets:
        return "--boundary only suppresses street ends, so it needs --streets"
    numbers = [v for v in (args.overlap_tol, args.gap_tol, args.min_gap_width,
                           args.snap_tol, args.isolated_tol, args.node_tol,
                           args.bin, args.min_boundary_cover) if v is not None]
    if not all(math.isfinite(v) for v in numbers):
        return ("every threshold must be a finite number. A nan or inf makes "
                "each comparison false and hides real findings.")
    if args.sample < 0 or args.bin <= 0:
        return "--sample cannot be negative and --bin must be positive"
    if min(args.overlap_tol, args.gap_tol, args.min_gap_width,
           args.min_boundary_cover) < 0 or args.min_boundary_cover > 1:
        return ("thresholds cannot be negative and --min-boundary-cover is a "
                "share from 0 to 1")
    if args.node_tol <= 0 or args.snap_tol <= args.node_tol:
        return "--node-tol must be positive and under --snap-tol"
    if args.isolated_tol is None:
        args.isolated_tol = args.snap_tol
    if args.isolated_tol < args.snap_tol:
        return "--isolated-tol cannot be smaller than --snap-tol"
    if args.apply and not args.out:
        return "--apply needs --out"
    if args.out:
        for path in (args.polygons, args.streets, args.boundary):
            if path and os.path.normcase(os.path.realpath(path)) == \
                    os.path.normcase(os.path.realpath(args.out)):
                return ("--out %s is also an input layer. Name a different "
                        "file." % path)
    return None


def _tolerate_odd_characters():
    """Make print() unable to fail on a character the console cannot encode."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")


def main(argv=None):
    """Run the CLI. An error this code did not anticipate exits 70, because 1
    is the code a scheduled job reads as 'a gate failed'."""
    _tolerate_odd_characters()
    try:
        return _run(argv)
    except Exception as exc:
        print("error: unexpected %s: %s. This is a fault in the tool, not a "
              "finding about the data." % (type(exc).__name__, exc),
              file=sys.stderr)
        traceback.print_exc()
        return EXIT_CRASH


def _run(argv):
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        # argparse exits 2 on a flag it refuses, and 2 here means a file could
        # not be read. A mistyped --ap is a usage error, so it gets 64.
        return EXIT_USAGE if exc.code == 2 else exc.code
    try:
        load_libs()
    except MissingLibs as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_LIBS
    if args.self_test:
        return self_test()
    problem = _usage_problem(args)
    if problem:
        print("error: %s" % problem, file=sys.stderr)
        return EXIT_USAGE
    try:
        sections, notes, compared, empty = _analyse(args)
    except Refusal as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return EXIT_REFUSED
    except (IOError, OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_IO
    if not compared:
        print("error: every layer was empty, so nothing was compared.",
              file=sys.stderr)
        return EXIT_NOTHING
    for line in describe(sections, notes, args.sample):
        print(line)
    if args.out:
        if args.apply:
            try:
                write_findings(args.out, sections)
            except (IOError, OSError, ValueError, csv.Error) as exc:
                print("error: could not write %s: %s" % (args.out, exc),
                      file=sys.stderr)
                return EXIT_IO
            print("\nwrote %s" % args.out)
        else:
            print("\nCheck only. %s was not written. Re-run with --apply."
                  % args.out)
    if empty:
        print("error: %s held no features, so its check did not run. The "
              "report above covers only the other layer." % " and ".join(empty),
              file=sys.stderr)
        return EXIT_NOTHING
    return gate_exit(sections)


def _analyse(args):
    """Load, vet and check. Returns (sections, notes, features compared,
    flags of the named layers that held no features)."""
    work = crs_facts(args.work_epsg)
    problem = work_crs_problem(work)
    if problem:
        raise Refusal(problem)
    sections, notes, compared, empty = [], [], 0, []
    if args.polygons:
        feats, got = prepare_layer(args.polygons, args.polygons_epsg, work,
                                   args.polygons, "polygon", args.key_field)
        notes.extend(got)
        compared += len(feats)
        if feats:
            sections.append(overlap_section(feats, args.overlap_tol))
            sections.append(gap_section(feats, args.gap_tol,
                                        args.min_gap_width))
        else:
            empty.append("--polygons")
    if args.streets:
        lines, got = prepare_layer(args.streets, args.streets_epsg, work,
                                   args.streets, "line",
                                   args.street_key_field)
        notes.extend(got)
        compared += len(lines)
        boundary = None
        if lines and args.boundary:
            bfeats, got = prepare_layer(args.boundary, args.boundary_epsg,
                                        work, args.boundary, "polygon",
                                        args.key_field)
            notes.extend(got)
            if not bfeats:
                raise Refusal("%s has no polygons, so it cannot suppress "
                              "anything" % args.boundary)
            boundary = shapely.unary_union([f.geom for f in bfeats])
            reasons = vet_boundary(boundary, endpoints_of(lines),
                                   args.snap_tol, args.min_boundary_cover)
            if reasons:
                raise Refusal("%s cannot be trusted: %s"
                              % (args.boundary, "; ".join(reasons)))
        if lines:
            sections.append(snap_section(lines, args, boundary))
        else:
            empty.append("--streets")
    return sections, notes, compared, empty


if __name__ == "__main__":
    sys.exit(main())
