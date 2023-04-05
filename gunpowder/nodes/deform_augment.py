import logging
import math
import numpy as np
import random
from scipy import ndimage

from .batch_filter import BatchFilter
from gunpowder.batch import Batch
from gunpowder.batch_request import BatchRequest
from gunpowder.coordinate import Coordinate
from gunpowder.ext import augment
from gunpowder.roi import Roi
from gunpowder.array import ArrayKey, Array
from gunpowder.array_spec import ArraySpec

logger = logging.getLogger(__name__)

# TODO: Add half voxel to points


class DeformAugment(BatchFilter):
    """Elasticly deform a batch. Requests larger batches upstream to avoid data
    loss due to rotation and jitter.

    Args:

        control_point_spacing (``tuple`` of ``int``):

            Distance between control points for the elastic deformation, in
            physical units per dimension.

        jitter_sigma (``tuple`` of ``float``):

            Standard deviation of control point jitter distribution, in physical units
            per dimension.

        scale_interval (``tuple`` of two ``floats``):

            Interval to randomly sample scale factors from.

        subsample (``int``):

            Instead of creating an elastic transformation on the full
            resolution, create one subsampled by the given factor, and linearly
            interpolate to obtain the full resolution transformation. This can
            significantly speed up this node, at the expense of having visible
            piecewise linear deformations for large factors. Usually, a factor
            of 4 can savely by used without noticable changes. However, the
            default is 1 (i.e., no subsampling).

        spatial_dims (``int``):

            The number of spatial dimensions in arrays. Spatial dimensions are
            assumed to be the last ones and cannot be more than 3 (default).
            Set this value here to avoid treating channels as spacial
            dimension. If, for example, your array is indexed as ``(c,y,x)``
            (2D plus channels), you would want to set ``spatial_dims=2`` to
            perform the elastic deformation only on x and y.

        use_fast_points_transform (``bool``):

            By solving for all of your points simultaneously with the following
            3 step proceedure:
            1) Rasterize nodes into numpy array
            2) Apply elastic transform to array
            3) Read out nodes via center of mass of transformed points
            You can gain substantial speed up as opposed to calculating the
            elastic transform for each point individually. However this may
            lead to nodes being lost during the transform.

        recompute_missing_points (``bool``):

            Whether or not to compute the elastic transform node wise for nodes
            that were lossed during the fast elastic transform process.
    """

    def __init__(
        self,
        control_point_spacing,
        jitter_sigma,
        scale_interval=(1.0, 1.0),
        subsample=1,
        spatial_dims=3,
        use_fast_points_transform=False,
        recompute_missing_points=True,
        transform_key: ArrayKey = None,
        graph_raster_voxel_size: Coordinate = None,
    ):
        self.control_point_spacing = Coordinate(control_point_spacing)
        self.jitter_sigma = jitter_sigma
        self.scale_min = scale_interval[0]
        self.scale_max = scale_interval[1]
        self.subsample = subsample
        self.spatial_dims = spatial_dims
        self.use_fast_points_transform = use_fast_points_transform
        self.recompute_missing_points = recompute_missing_points
        self.transform_key = transform_key
        self.graph_raster_voxel_size = Coordinate(graph_raster_voxel_size)

    def setup(self):
        if self.transform_key is not None:
            upstream_roi = self.spec.get_total_roi().snap_to_grid(
                self.control_point_spacing, mode="shrink"
            )
            spec = ArraySpec(
                roi=upstream_roi,
                voxel_size=self.control_point_spacing,
                interpolatable=True,
            )

            self.provides(self.transform_key, spec)

    def prepare(self, request):
        seed = request.random_seed
        random.seed(seed)
        np.random.seed(seed)

        # get the total ROI of all requests
        total_roi = request.get_total_roi()
        logger.debug("total ROI is %s" % total_roi)

        # First, get the total ROI of the request in spatial dimensions only.
        # Channels and time don't matter. This is our master ROI.

        # get master ROI
        master_roi = Roi(
            total_roi.begin[-self.spatial_dims :],
            total_roi.shape[-self.spatial_dims :],
        )
        self.spatial_dims = master_roi.dims
        logger.debug("master ROI is %s" % master_roi)

        # make sure the master ROI aligns with the control point spacing
        master_roi_snapped = master_roi.snap_to_grid(
            self.control_point_spacing, mode="grow"
        )
        logger.debug(
            "master ROI aligned with control points is %s" % master_roi_snapped
        )

        # get master roi in control point spacing
        master_roi_sampled = master_roi_snapped / self.control_point_spacing
        logger.debug("master ROI in control point spacing is %s" % master_roi_sampled)

        # Second, create a master transformation. This is a transformation that
        # covers all voxels of the all requested ROIs. The master transformation
        # is zero-based, all transformations are relative to the origin of master_roi_snapped
        self.master_transformation_spec = ArraySpec(
            master_roi_snapped, self.control_point_spacing, interpolatable=True
        )
        (
            self.master_transformation,
            self.local_transformation,
        ) = self.__create_transformation(self.master_transformation_spec)

        # Third, sample the master transformation for each of the
        # smaller requested ROIs at their respective voxel resolution.
        # crop the parts corresponding to the requested ROIs
        self.transformations = {}
        deps = BatchRequest()
        for key, spec in request.items():
            if key == self.transform_key:
                continue
            spec = spec.copy()

            if spec.roi is None:
                continue

            # get target roi and target spacing (voxel size for arrays or just control point
            # spacing for graphs)
            target_roi = Roi(
                spec.roi.begin[-self.spatial_dims :],
                spec.roi.shape[-self.spatial_dims :],
            )

            if isinstance(key, ArrayKey):
                voxel_size = Coordinate(self.spec[key].voxel_size[-self.spatial_dims :])
            else:
                # must select voxel size for the graph spec because otherwise we would
                # interpolate the transformation onto a spacing of 1 which may be
                # way too large
                voxel_size = self.graph_raster_voxel_size
                assert (
                    voxel_size is not None
                ), "Please provide a graph_raster_voxel_size when deforming graphs"

            # we save transformations that have been sampled for specific ROI's and voxel sizes,
            # no need to recompute. This can save time if you are requesting multiple arrays of
            # the same voxel size and shape
            if (
                target_roi.offset,
                target_roi.shape,
                voxel_size,
            ) in self.transformations:
                transformation = self.transformations[
                    (target_roi.offset, target_roi.shape, voxel_size)
                ]
            else:
                # sample the master transformation at the voxel spacing of each array
                transformation = self.__sample_transform(
                    self.master_transformation,
                    voxel_size,
                    target_roi.snap_to_grid(voxel_size),
                )
                self.transformations[
                    (target_roi.offset, target_roi.shape, voxel_size)
                ] = transformation

            # get ROI of all control points necessary to perform transformation
            #
            # for that we follow the same transformations to get from the
            # request ROI to the target ROI in master ROI in control points, just in
            # reverse
            source_roi = self.__get_source_roi(transformation)

            # update upstream request
            spec.roi = Roi(
                spec.roi.begin[: -self.spatial_dims]
                + source_roi.begin[-self.spatial_dims :],
                spec.roi.shape[: -self.spatial_dims]
                + source_roi.shape[-self.spatial_dims :],
            )

            deps[key] = spec

            logger.debug("upstream request roi for %s = %s" % (key, spec.roi))

        return deps

    def process(self, batch, request):
        out_batch = Batch()
        for array_key, array in batch.arrays.items():
            request_roi = Roi(
                request[array_key].roi.offset[-self.spatial_dims :],
                request[array_key].roi.shape[-self.spatial_dims :],
            )
            voxel_size = Coordinate(array.spec.voxel_size[-self.spatial_dims :])
            assert (
                request_roi.offset,
                request_roi.shape,
                voxel_size,
            ) in self.transformations, f"{(request_roi.offset, request_roi.shape, voxel_size)} not in {list(self.transformations.keys())}"

            # reshape array data into (channels,) + spatial dims
            transformed_array = self.__apply_transform(
                array,
                self.transformations[
                    (request_roi.offset, request_roi.shape, voxel_size)
                ],
            )

            out_batch[array_key] = transformed_array

        for graph_key, graph in batch.graphs.items():
            source_roi = Roi(
                request[graph_key].roi.offset[-self.spatial_dims :],
                request[graph_key].roi.shape[-self.spatial_dims :],
            )
            nodes = list(graph.nodes)

            if self.use_fast_points_transform:
                missed_nodes = self.__fast_point_projection(
                    self.transformations[
                        source_roi.offset,
                        source_roi.shape,
                        self.graph_raster_voxel_size,
                    ],
                    nodes,
                    graph.spec.roi,
                    target_roi=source_roi,
                )
                if not self.recompute_missing_points:
                    for node in set(missed_nodes):
                        graph.remove_node(node, retain_connectivity=True)
                    missed_nodes = []
            else:
                missed_nodes = nodes

            for node in missed_nodes:
                # logger.debug("projecting %s", node.location)

                # get location relative to beginning of upstream ROI
                location = node.location

                # get spatial coordinates of node
                location_spatial = location[-self.spatial_dims :]

                # get projected location in transformation data space, this
                # yields voxel coordinates relative to target ROI
                projected = self.__project(
                    self.transformations[
                        source_roi.offset,
                        source_roi.shape,
                        self.graph_raster_voxel_size,
                    ],
                    location_spatial,
                )

                logger.debug("projected: %s", projected)

                if projected is None:
                    logger.debug("node outside of target, skipping")
                    graph.remove_node(node, retain_connectivity=True)
                    continue

                # update spatial coordinates of node location
                node.location[-self.spatial_dims :] = projected

                logger.debug("final location: %s", node.location)

            out_batch[graph_key] = graph

        if self.transform_key is not None:
            out_batch[self.transform_key] = self.local_transformation

        return out_batch

    def __apply_transform(self, array: Array, transformation: Array) -> Array:
        input_shape = array.data.shape
        output_shape = transformation.data.shape
        channel_shape = input_shape[: -self.spatial_dims]
        data = array.data.reshape((-1,) + input_shape[-self.spatial_dims :])

        offset = array.spec.roi.offset[-self.spatial_dims :]
        voxel_size = array.spec.voxel_size[-self.spatial_dims :]

        # apply transformation on each channel
        transformation.data -= np.array(offset).reshape(
            (-1,) + (1,) * self.spatial_dims
        )
        transformation.data /= np.array(voxel_size).reshape(
            (-1,) + (1,) * self.spatial_dims
        )

        data = np.array(
            [
                augment.apply_transformation(
                    data[c],
                    transformation.data,
                    interpolate=array.spec.interpolatable,
                )
                for c in range(data.shape[0])
            ]
        )
        spec = array.spec.copy()
        spec.roi = transformation.spec.roi

        return Array(
            data.reshape(channel_shape + output_shape[-self.spatial_dims :]), spec
        )

    def __sample_transform(
        self,
        transformation: Array,
        output_voxel_size,
        output_roi,
        interpolate_order=1,
    ) -> Array:
        if output_voxel_size == transformation.spec.voxel_size:
            # if voxel_size == control_point_spacing we can simply slice into the master roi
            relative_output_roi = (
                output_roi - transformation.spec.roi.offset
            ).snap_to_grid(output_voxel_size) / output_voxel_size
            sampled = np.copy(
                transformation.data[
                    (slice(None),) + relative_output_roi.get_bounding_box()
                ]
            )
            return Array(
                sampled,
                ArraySpec(
                    output_roi.snap_to_grid(output_voxel_size),
                    output_voxel_size,
                    interpolatable=True,
                ),
            )

        dims = len(output_voxel_size)
        output_shape = output_roi.shape / output_voxel_size
        offset = np.array(
            [
                o / s
                for o, s in zip(
                    output_roi.offset - transformation.spec.roi.offset,
                    transformation.spec.voxel_size,
                )
            ]
        )
        step = np.array(
            [o / i for o, i in zip(output_voxel_size, transformation.spec.voxel_size)]
        )
        coordinates = np.meshgrid(
            range(dims),
            *[
                np.linspace(o, (shape - 1) * step + o, shape)
                for o, shape, step in zip(offset, output_shape, step)
            ],
            indexing="ij",
        )
        coordinates = np.stack(coordinates)

        sampled = ndimage.map_coordinates(
            transformation.data,
            coordinates=coordinates,
            order=3,
            mode="nearest",
        )
        return Array(sampled, ArraySpec(output_roi, output_voxel_size))

    def __create_transformation(self, target_spec: ArraySpec):
        scale = self.scale_min + random.random() * (self.scale_max - self.scale_min)

        target_shape = target_spec.roi.shape / target_spec.voxel_size

        global_transformation = augment.create_identity_transformation(
            target_shape,
            subsample=self.subsample,
            scale=scale,
        )
        local_transformation = np.zeros_like(global_transformation)

        if sum(self.jitter_sigma) > 0:
            el_transformation = augment.create_elastic_transformation(
                target_shape,
                1,
                np.array(self.jitter_sigma) / self.control_point_spacing,
                subsample=self.subsample,
            )

            local_transformation += el_transformation

        if self.subsample > 1:
            local_transformation = augment.upscale_transformation(
                local_transformation, target_shape
            )

        # transform into world units
        global_transformation *= np.array(target_spec.voxel_size).reshape(
            (len(target_spec.voxel_size),) + (1,) * self.spatial_dims
        )
        global_transformation += np.array(target_spec.roi.offset).reshape(
            (len(target_spec.roi.offset),) + (1,) * self.spatial_dims
        )

        local_transformation *= np.array(target_spec.voxel_size).reshape(
            (len(target_spec.voxel_size),) + (1,) * self.spatial_dims
        )

        return (
            Array(global_transformation + local_transformation, target_spec),
            Array(local_transformation, target_spec),
        )

    def __fast_point_projection(self, transformation, nodes, source_roi, target_roi):
        if len(nodes) < 1:
            return []
        # rasterize the points into an array
        ids, locs = zip(
            *[
                (
                    node.id,
                    (np.floor(node.location).astype(int) - source_roi.begin)
                    // self.graph_raster_voxel_size,
                )
                for node in nodes
                if source_roi.contains(node.location)
            ]
        )
        ids, locs = np.array(ids), tuple(zip(*locs))
        points_array = np.zeros(
            source_roi.shape / self.graph_raster_voxel_size, dtype=np.int64
        )
        points_array[locs] = ids

        # reshape array data into (channels,) + spatial dims
        shape = points_array.shape
        data = points_array.reshape((-1,) + shape[-self.spatial_dims :])

        array = Array(
            data,
            ArraySpec(
                Roi(
                    source_roi.begin[-self.spatial_dims :],
                    Coordinate(shape) * self.graph_raster_voxel_size,
                ),
                self.graph_raster_voxel_size,
            ),
        )
        transformed = self.__apply_transform(array, transformation)

        data = transformed.data
        missing_points = []
        projected_locs = ndimage.measurements.center_of_mass(data > 0, data, ids)
        projected_locs = [
            np.array(loc[-self.spatial_dims :]) * self.graph_raster_voxel_size
            + target_roi.begin
            for loc in projected_locs
        ]
        node_dict = {node.id: node for node in nodes}
        for point_id, proj_loc in zip(ids, projected_locs):
            point = node_dict.pop(point_id)
            if not any([np.isnan(x) for x in proj_loc]):
                assert (
                    len(proj_loc) == self.spatial_dims
                ), "projected location has wrong number of dimensions: {}, expected: {}".format(
                    len(proj_loc), self.spatial_dims
                )
                point.location[-self.spatial_dims :] = proj_loc
            else:
                missing_points.append(point)
        for node in node_dict.values():
            missing_points.append(point)
        logger.debug(
            "{} of {} points lost in fast points projection".format(
                len(missing_points), len(ids)
            )
        )

        return missing_points

    def __project(self, transformation: Array, location: np.ndarray) -> np.ndarray:
        """Find the projection of location given by transformation. Returns None
        if projection lies outside of transformation."""

        dims = len(location)

        # subtract location from transformation
        diff = transformation.data.copy()
        for d in range(dims):
            diff[d] -= location[d]

        # square
        diff2 = diff * diff

        # sum
        dist = diff2.sum(axis=0)

        # find grid point closes to location
        center_grid = Coordinate(np.unravel_index(dist.argmin(), dist.shape))
        center_source = self.__source_at(transformation, center_grid)

        logger.debug("projecting %s onto grid", location)
        logger.debug("grid shape: %s", transformation.data.shape[1:])
        logger.debug("grid projection: %s", center_grid)
        logger.debug("dist shape: %s", dist.shape)
        logger.debug("dist.argmin(): %s", dist.argmin())
        logger.debug("dist[argmin]: %s", dist[center_grid])
        logger.debug(
            "transform[argmin]: %s", transformation.data[(slice(None),) + center_grid]
        )
        logger.debug("min dist: %s", dist.min())
        logger.debug("center source: %s", center_source)

        # inspect grid edges incident to center_grid
        for d in range(dims):
            # nothing to do for dimensions without spatial extent
            if transformation.data.shape[1 + d] == 1:
                continue

            dim_vector = Coordinate(1 if dd == d else 0 for dd in range(dims))
            pos_grid = center_grid + dim_vector
            neg_grid = center_grid - dim_vector
            logger.debug("interpolating along %s", dim_vector)

            pos_u = -1
            neg_u = -1

            if pos_grid[d] < transformation.data.shape[1 + d]:
                pos_source = self.__source_at(transformation, pos_grid)
                logger.debug("pos source: %s", pos_source)
                pos_dist = pos_source[d] - center_source[d]
                loc_dist = location[d] - center_source[d]
                if pos_dist != 0:
                    pos_u = loc_dist / pos_dist
                else:
                    pos_u = 0

            if neg_grid[d] >= 0:
                neg_source = self.__source_at(transformation, neg_grid)
                logger.debug("neg source: %s", neg_source)
                neg_dist = neg_source[d] - center_source[d]
                loc_dist = location[d] - center_source[d]
                if neg_dist != 0:
                    neg_u = loc_dist / neg_dist
                else:
                    neg_u = 0

            logger.debug("pos u/neg u: %s/%s", pos_u, neg_u)

            # if a point only falls behind edges, it lies outside of the grid
            if pos_u < 0 and neg_u < 0:
                return None

        return (
            np.array(center_grid, dtype=np.float32) * transformation.spec.voxel_size
            + transformation.spec.roi.offset
        )

    def __source_at(self, transformation, index):
        """Read the source point of a transformation at index."""

        slices = (slice(None),) + tuple(slice(i, i + 1) for i in index)
        return transformation.data[slices].flatten()

    def __get_source_roi(self, transformation):
        # this gets you the source_roi in offset space. We need to add 1 voxel
        # to the shape to get the closed interval ROI

        # get bounding box of needed data for transformation
        bb_min = Coordinate(
            int(math.floor(transformation.data[d].min()))
            for d in range(transformation.spec.voxel_size.dims)
        )
        bb_max = Coordinate(
            int(math.ceil(transformation.data[d].max())) + s
            for d, s in zip(
                range(transformation.spec.voxel_size.dims),
                transformation.spec.voxel_size,
            )
        )

        # create roi sufficiently large to feed transformation
        source_roi = Roi(bb_min, bb_max - bb_min).snap_to_grid(
            transformation.spec.voxel_size
        )

        return source_roi

    def __shift_transformation(self, shift, transformation):
        for d in range(transformation.shape[0]):
            transformation[d] += shift[d]
