import shapely
import pandas as pd
import networkx as nx

from tqdm import tqdm
from syspy.spatial import polygons, spatial
from syspy.syspy_utils import neighbors, syscolors


def merge_zonings(background, foreground, min_area_factor=0.01, min_area=None):

    back = background.copy()
    front = foreground.copy()

    stencil = shapely.geometry.MultiPolygon(
        list(front['geometry'])
    ).buffer(1e-9)

    back['geometry'] = back['geometry'].apply(lambda g: g.difference(stencil))
    back['geometry'] = polygons.biggest_polygons(list(back['geometry']))

    back['area'] = [g.area for g in back['geometry']]
    min_area = min_area if min_area else back['area'].mean() * min_area_factor

    back = back.loc[back['area'] > min_area]

    back['id'] = back.index
    front['id'] = front.index
    back['zoning'] = 'back'
    front['zoning'] = 'front'

    columns = ['zoning', 'id', 'geometry']

    concatenated = pd.concat(
        [back[columns], front[columns]]
    )

    df = concatenated

    zones = list(df['geometry'])
    clean_zones = polygons.clean_zoning(
        zones,
        buffer=1e-4,
        fill_buffer=2e-3,
        fill_gaps=False,
        unite_gaps=True
    )
    df['geometry'] = clean_zones

    return df.reset_index(drop=True)


def pool_and_geometries(pool, geometries):
    done = []

    while len(pool):
        # start another snail
        done.append(pool[0])
        current = geometries[pool[0]]
        pool = [p for p in pool if p not in done]

        for i in range(len(pool)):
            for p in pool:
                if geometries[p].intersects(current):
                    done.append(p)
                    current = geometries[p]
                    pool = [p for p in pool if p not in done]
                    break

    return done


def snail_number(zones, center):
    distance_series = zones['geometry'].apply(lambda g: center.distance(g))
    distance_series.name = 'cluster_distance'
    distance_series.sort_values(inplace=True)
    geometries = zones['geometry'].to_dict()

    pool = list(distance_series.index)

    done = pool_and_geometries(pool, geometries)

    snail = pd.Series(done)
    snail.index.name = 'cluster_snail'
    snail.name = 'cluster'

    indexed = snail.reset_index().set_index('cluster')['cluster_snail']

    return indexed.loc[zones.index] # we use zones.index to sort the result


def cluster_snail_number(zones, n_clusters=20, buffer=1e-6):
    # zones can be a series or a list

    zones = pd.DataFrame(pd.Series(zones))
    df = zones.copy()

    # we want the geometries to intersect each other

    # the total area of the zoning
    union = shapely.geometry.MultiPolygon(
        list(df['geometry'])
    ).buffer(buffer)

    center = union.centroid

    clusters, cluster_series = spatial.zone_clusters(df, n_clusters=n_clusters)
    df['cluster'] = cluster_series

    distance_series = clusters['geometry'].apply(lambda g: center.distance(g))
    distance_series.name = 'cluster_distance'

    distance_series.sort_values(inplace=True)
    geometries = clusters['geometry'].to_dict()

    snail = snail_number(clusters, center)

    clusters['snail'] = snail

    df = pd.merge(df, snail.reset_index(), on='cluster')
    df['distance'] = df['geometry'].apply(lambda g: center.distance(g))

    sorted_df = df.sort_values(by=['cluster_snail', 'distance'])

    to_concat = []

    for cluster in set(df['cluster']):

        proto = sorted_df.copy()
        proto = proto.loc[proto['cluster'] == cluster]
        geometries = proto['geometry'].apply(
            lambda g: g.buffer(buffer)).to_dict()
        pool = list(proto.index)

        done = pool_and_geometries(pool, geometries)

        snail = pd.Series(done)
        snail.index.name = 'snail'
        snail.name = 'original_index'

        proto.index.name = 'original_index'
        proto.reset_index(inplace=True)
        proto = pd.merge(proto, snail.reset_index(), on='original_index')

        to_concat.append(proto)

    concat = pd.concat(to_concat)

    df = concat.copy()
    df.sort_values(by=['cluster_snail', 'snail'], inplace=True)

    df.reset_index(inplace=True, drop=True)
    df.reset_index(inplace=True, drop=False)

    if True:
        df.drop('geometry', inplace=True, axis=1)
        df = pd.merge(
            df,
            sorted_df[['geometry']],
            left_on='original_index',
            right_index=True
        )

    #
    df.set_index('original_index', inplace=True)

    return df.loc[zones.index]


def greedy_color(zoning, colors=syscolors.rainbow_shades, buffer=1e-6):
    zoning = zoning.copy()
    zoning['geometry'] = zoning['geometry'].apply(lambda g: g.buffer(buffer))

    # TODO change the edge construction to make it independant from neighbors
    n = neighbors.neighborhood_dataframe(zoning)
    edges = n[['origin', 'destination']].values

    g = nx.Graph()
    g.add_edges_from(edges)
    d = nx.coloring.greedy_color(
        g,
        strategy=nx.coloring.strategy_largest_first
    )

    color_list = list(colors)

    def index_to_color(index):
        return color_list[index]

    return pd.Series(d).apply(index_to_color)

########################################################################


def intersection_area(geoa, geob):

    if geoa.intersects(geob):
        intersection = geoa.intersection(geob)
        return intersection.area
    else:
        return 0


def intersection_area_matrix(x_geometries, y_geometries):
    array = []
    for g in tqdm(x_geometries, desc=str(len(y_geometries))):
        array.append(
            [
                intersection_area(y_geometry, g)
                for y_geometry in y_geometries
            ]
        )
    return array


def intersection_area_dataframe(front, back):
    front.index.name = 'front_index'
    back.index.name = 'back_index'
    ia_matrix = intersection_area_matrix(
        list(front['geometry']),
        list(back['geometry'])
    )

    df = pd.DataFrame(ia_matrix)
    df.index = front.index
    df.columns = back.index

    return df


def front_distribution(front_zone, intersection_dataframe):
    """
    share of the front zone in intersection with every back zone
    """
    df = intersection_dataframe
    intersection_series = df.loc[front_zone]
    area = intersection_series.sum()
    return intersection_series / area


def back_distribution(front_zone, intersection_dataframe):
    df = intersection_dataframe
    """
    share of of every back zone in intersection with the front zone
    """
    intersection_series = df.loc[front_zone]
    area_series = df.sum()
    return intersection_series / area_series


def share_intensive_columns(front_zone, back, intersection_dataframe, columns):
    shares = front_distribution(front_zone, intersection_dataframe)
    shared_series = back[columns].apply(lambda s: s*shares)
    return shared_series.sum()


def share_extensive_columns(front_zone, back, intersection_dataframe, columns):
    shares = back_distribution(front_zone, intersection_dataframe)
    shared_series = back[columns].apply(lambda s: s*shares)
    return shared_series.sum()


def concatenate_back_columns_to_front(front, back, intensive, extensive):
    df = intersection_area_dataframe(front, back)
    apply_series = pd.Series(front.index, index=front.index)

    intensive_dataframe = apply_series.apply(
        lambda z: share_extensive_columns(z, back, df, intensive)
    )
    extensive_dataframe = apply_series.apply(
        lambda z: share_extensive_columns(z, back, df, extensive)
    )
    return pd.concat(
        [front, intensive_dataframe, extensive_dataframe],
        axis=1
    )


def normalize_columns(df):
    column_sums = df.sum()
    normalized = df / column_sums
    return normalized


def share_od_extensive_columns(
    od_dataframe,
    intersection_dataframe,
    extensive_columns
):
    normalized = normalize_columns(intersection_dataframe)
    # series (front, back) -> normalized_intersection
    stack = normalized.stack()

    origin_stack = stack.loc[stack > 0].copy()
    destination_stack = stack.loc[stack > 0].copy()
    origin_stack.index.names = ['front_index_origin', 'back_index_origin']
    dest_index_names = ['front_index_destination', 'back_index_destination']
    destination_stack.index.names = dest_index_names

    # dense matrix of OD shares (origin_share * destination_share)
    share_matrix = origin_stack.apply(lambda v: v*destination_stack)
    share_matrix = share_matrix.sort_index(axis=0).sort_index(axis=1)

    # we stack the two columns index
    share_stack = share_matrix.stack(dest_index_names)
    share_stack.name = 'shares'
    share_stack = share_stack.reset_index()

    pool = od_dataframe.rename(
        columns={
            'origin':'back_index_origin',
            'destination':'back_index_destination'
        }
    )

    # we expen the od_dataframe by mergint it on the shares
    merged = pd.merge(
        pool,
        share_stack,
        on=['back_index_origin', 'back_index_destination']
    )
    print(len(merged))

    # we reduce merged by grouping it by front indexes,
    # multiplying each row by its' share
    shared = merged.copy()
    shared[extensive_columns] = shared[extensive_columns].apply(
        lambda c: c*shared['shares'])

    grouped = shared.groupby(
        ['front_index_origin', 'front_index_destination'],
    )
    extensive_sums = grouped[extensive_columns].sum()
    extensive_sums.index.names = ['origin', 'destination']

    return extensive_sums.reset_index()
