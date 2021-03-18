import numpy, pandas
from scipy.spatial import distance
from sklearn.utils import check_array
from scipy.stats import mode as most_common_value
import matplotlib.pyplot as plt
from tqdm import tqdm


def _resolve_metric(X, coordinates, metric):
    """
    Provide a distance function that you can use to find the distance betwen arbitrary points.
    """
    if callable(metric):
        distance_func = metric
    elif metric.lower() == "haversine":
        try:
            from numba import autojit
        except:

            def autojit(func):
                return func

        @autojit
        def harcdist(p1, p2):
            """ Compute the kernel of haversine"""
            x = numpy.sin(p2[1] - p1[1] / 2) ** 2
            y = (
                numpy.cos(p2[1])
                * numpy.cos(p1[1])
                * numpy.sin((p2[0] - p1[0]) / 2) ** 2
            )
            return 2 * numpy.arcsin(numpy.sqrt(x + y))

        distance_func = harcdist
    elif metric.lower() == "precomputed":
        # so, in this case, coordinates is actually distance matrix of some kind
        # and is assumed aligned to X, such that the distance from X[a] to X[b] is
        # coordinates[a,b], confusingly... So, we'll re-write them as "distances"
        distances = check_array(coordinates, accept_sparse=True)
        n, k = distances.shape
        assert k == n, (
            'With metric="precomputed", coordinates must be an (n,n) matrix'
            " representing distances between coordinates."
        )

        def lookup_distance(a, b):
            """ Find location of points a,b in X and return precomputed distances"""
            (aloc,) = (X == a).all(axis=1).nonzero()
            (bloc,) = (X == b).all(axis=1).nonzero()
            if (len(aloc) > 1) or (len(bloc) > 1):
                raise NotImplementedError(
                    "Precomputed distances cannot disambiguate coincident points."
                    " Add a slight bit of noise to the input to force them"
                    " into non-coincidence and re-compute the distance matrix."
                )
            elif (len(aloc) == 0) or (len(bloc) == 0):
                raise NotImplementedError(
                    "Precomputed distances cannot compute distances to new points."
                )
            return distances[aloc, bloc]

        distance_func = lookup_distance
    else:
        try:
            distance_func = getattr(distance, metric)
        except AttributeError:
            raise KeyError(
                "Metric {} not understood. Choose "
                "something available in scipy.spatial.distance".format(metric)
            )
    return distance_func


def isolation(X, coordinates, metric="euclidean", middle="median", return_all=False):
    X = check_array(X, ensure_2d=False)
    X = to_elevation(X, middle=middle).squeeze()
    try:
        from rtree.index import Index as SpatialIndex
    except ImportError:
        raise ImportError(
            "rtree library must be installed to use " "the prominence measure"
        )
    distance_func = _resolve_metric(X, coordinates, metric)
    sort_order = numpy.argsort(-X)
    tree = SpatialIndex()
    tree.insert(0, tuple(coordinates[sort_order][0]), obj=X[sort_order][0])
    ix = numpy.where(sort_order == 0)[0].item()
    precedence_tree = [[ix, numpy.nan, 0, numpy.nan, numpy.nan, numpy.nan]]
    for i, (value, location) in enumerate(
        zip(X[sort_order][1:], coordinates[sort_order][1:])
    ):
        rank = i + 1
        ix = numpy.where(sort_order == rank)[0].item()
        (match,) = tree.nearest(tuple(location), objects=True)
        higher_rank = match.id
        higher_value = match.object
        higher_location = match.bbox[:2]
        higher_ix = numpy.where(sort_order == higher_rank)[0].item()
        distance = distance_func(location, higher_location)
        gap = higher_value - value
        precedence_tree.append([ix, higher_ix, rank, higher_rank, distance, gap])
        tree.insert(rank, tuple(location), obj=value)
    # return precedence_tree
    precedence_tree = numpy.asarray(precedence_tree)
    # print(precedence_tree.shape)
    out = numpy.empty_like(precedence_tree)
    out[sort_order] = precedence_tree
    isolation = pandas.DataFrame(
        out, columns=["index", "parent_index", "rank", "parent_rank", "distance", "gap"]
    )
    if return_all:
        return isolation
    else:
        return isolation.distance.values


def prominence(
    X,
    connectivity,
    return_saddles=False,
    return_peaks=False,
    return_dominating_peak=False,
    gdf=None,
    verbose=False,
    middle="mean",
):
    X = check_array(X, ensure_2d=False).squeeze()
    X = to_elevation(X, middle=middle).squeeze()
    (n,) = X.shape

    # sort the variable in ascending order
    sort_order = numpy.argsort(-X)

    peaks = [sort_order[0]]
    assessed_peaks = set()
    prominence = numpy.empty_like(X) * numpy.nan
    dominating_peak = numpy.ones_like(X) * -1
    predecessors = numpy.ones_like(X) * -1
    key_cols = dict()
    for rank, value in tqdm(enumerate(X[sort_order])):
        # This is needed to break ties in the same way that argsort does. A more
        # natural way to do this is to use X >= value, but if value is tied, then
        # that would generate a mask where too many elements are selected!
        # e.g. mask.sum() > rank
        mask = numpy.isin(numpy.arange(n), sort_order[: rank + 1])
        (full_indices,) = mask.nonzero()
        this_full_ix = sort_order[rank]
        msg = "assessing {} (rank: {}, value: {})".format(this_full_ix, rank, value)

        # use the dominating_peak vector. A new obs either has:
        # 1. neighbors whose dominating_peak are all -1 (new peak)
        # 2. neighbors whose dominating_peak are all -1 or an integer (slope of current peak)
        # 3. neighbors whose dominating_peak include at least two integers and any -1 (key col)
        _, neighbs = connectivity[this_full_ix].toarray().nonzero()
        this_preds = predecessors[neighbs]

        # need to keep ordering in this sublist to preserve hierarchy
        this_unique_preds = [p for p in peaks if ((p in this_preds) & (p >= 0))]
        joins_new_subgraph = not set(this_unique_preds).issubset(assessed_peaks)
        if tuple(this_unique_preds) in key_cols.keys():
            classification = "slope"
        elif len(this_unique_preds) == 0:
            classification = "peak"
        elif (len(this_unique_preds) >= 2) & joins_new_subgraph:
            classification = "keycol"
        else:
            classification = "slope"

        if (
            classification == "keycol"
        ):  # this_ix merges two or more subgraphs, so is a key_col
            # find the peaks it joins
            now_joined_peaks = this_unique_preds
            # add them as keys for the key_col lut
            key_cols.update({tuple(now_joined_peaks): this_full_ix})
            msg += "\n{} is a key col between {}!".format(
                this_full_ix, now_joined_peaks
            )
            dominating_peak[this_full_ix] = now_joined_peaks[
                -1
            ]  # lowest now-joined peak
            predecessors[this_full_ix] = now_joined_peaks[-1]
            prominence[this_full_ix] = 0
            # given we now know the key col, get the prominence for
            # unassayed peaks in the subgraph
            for peak_ix in now_joined_peaks:
                if peak_ix in assessed_peaks:
                    continue
                # prominence is peak - key col
                prominence[peak_ix] -= value
                assessed_peaks.update((peak_ix,))
        elif classification == "peak":  # this_ix is a new peak since it's disconnected
            msg += "\n{} is a peak!".format(this_full_ix)
            # its parent is the last visited peak (for precedence purposes)
            previous_peak = peaks[-1]
            if not (this_full_ix in peaks):
                peaks.append(this_full_ix)
            dominating_peak[this_full_ix] = previous_peak
            predecessors[this_full_ix] = this_full_ix
            # we initialize prominence here, rather than compute it solely in
            # the `key_col` branch because a graph `island` disconnected observation
            # should have prominence "value - 0", since it has no key cols
            prominence[this_full_ix] = X[this_full_ix]
        else:  # this_ix is connected to an existing peak, but doesn't bridge peaks.
            msg += "\n{} is a slope!".format(this_full_ix)
            # get all the peaks that are linked to this slope
            this_peak = this_unique_preds
            if len(this_peak) == 1:  # if there's only one peak the slope is related to
                # then use it
                best_peak = this_peak[0]
            else:  # otherwise, if there are multiple peaks
                # pick the one that most of its neighbors are assigned to
                best_peak = most_common_value(this_unique_preds).mode.item()
            all_on_slope = numpy.arange(n)[dominating_peak == best_peak]
            msg += "\n{} are on the slopes of {}".format(all_on_slope, best_peak)
            dominating_peak[this_full_ix] = best_peak
            predecessors[this_full_ix] = best_peak
        if verbose:
            print(
                "--------------------------------------------\n"
                "at the {} iteration:\n{}\n\tpeaks\t{}\n\tprominence\t{}\n\tkey_cols\t{}\n"
                "".format(rank, msg, peaks, prominence, key_cols)
            )
        if gdf is not None:
            peakframe = gdf.iloc[peaks]
            keycolframe = gdf.iloc[list(key_cols.values())]
            slopeframe = gdf[
                (~(gdf.index.isin(peakframe.index) | gdf.index.isin(keycolframe.index)))
                & mask
            ]
            rest = gdf[~mask]
            this_geom = gdf.iloc[[this_full_ix]]
            ax = rest.plot(edgecolor="k", linewidth=0.1, facecolor="lightblue")
            ax = slopeframe.plot(edgecolor="k", linewidth=0.1, facecolor="linen", ax=ax)
            ax = keycolframe.plot(edgecolor="k", linewidth=0.1, facecolor="red", ax=ax)
            ax = peakframe.plot(edgecolor="k", linewidth=0.1, facecolor="yellow", ax=ax)
            ax = this_geom.centroid.plot(ax=ax, color="orange", marker="*")
            plt.show()
            command = input()
            if command.strip().lower() == "stop":
                break
    if not any((return_saddles, return_peaks, return_dominating_peak)):
        return prominence
    retval = [prominence]
    if return_saddles:
        retval.append(key_cols)
    if return_peaks:
        retval.append(peaks)
    if return_dominating_peak:
        retval.append(dominating_peak)
    return retval


def to_elevation(X, middle="mean", metric="euclidean"):
    """
    Compute the "elevation" of coordinates in p-dimensional space.

    For 1 dimensional X, this simply sets the zero point at the minimum value
    for the data. As an analogue to physical elevation, this means that the
    lowest value in 1-dimensional X is considered "sea level."

    For X in higher dimension, we treat X as defining a location on a (hyper)sphere.
    The "elevation," then, is the distance from the center of mass.
    So, this computes the distance of each point to the overall the center of mass
    and uses this as the "elevation," setting sea level (zero) to the lowest elevation.

    Arguments
    ---------
    X : numpy.ndarray
        Array of values for which to compute elevation.
    middle : callable or string
        name of function in numpy (or function itself) used to compute the center point of X
    metric : string
        metric to use in `scipy.spatial.distance.cdist` to compute the distance from the center
        of mass to the point.

    Returns
    --------
    (N,1)-shaped numpy array containing the "elevation" of each point relative to sea level (zero).

    """
    if X.ndim == 1:
        return X - X.min()
    else:
        if callable(middle):
            middle_point = middle(X, axis=0)
        else:
            try:
                middle = getattr(numpy, middle)
                return to_elevation(X, middle=middle)
            except AttributeError:
                raise KeyError(
                    'numpy has no "{}" function to compute the middle'
                    " of a point cloud.".format(middle)
                )
        distance_from_center = distance.cdist(X, middle_point.reshape(1, -1))
        return distance_from_center


if __name__ == "__main__":
    import geopandas, pandas
    from libpysal import weights

    data = geopandas.read_file("../cb_2015_us_county_500k_2.geojson")
    contig = data.query('statefp not in ("02", "15", "43", "72")').reset_index()
    coordinates = numpy.column_stack((contig.centroid.x, contig.centroid.y))
    income = contig[["median_income"]].values.flatten()
    contig_graph = weights.Rook.from_dataframe(contig)
    # iso = isolation(income, coordinates, return_all=True)
    # contig.assign(isolation = iso.distance.values).plot('isolation')

    wa = contig.query('statefp == "53"').reset_index()
    wa_income = wa[["median_income"]].values
    wa_graph = weights.Rook.from_dataframe(wa)

    ca = contig.query('statefp == "06"').reset_index()
    ca_income = ca[["median_income"]].values
    ca_graph = weights.Rook.from_dataframe(ca)
