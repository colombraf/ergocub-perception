import copy
import sys
from pathlib import Path

import cv2
import numpy as np
from scipy.spatial.transform import Rotation
from loguru import logger

from utils.concurrency.utils.signals import Signals

sys.path.insert(0, Path(__file__).parent.parent.as_posix())

from grasping.utils.input import RealSense
from grasping.utils.misc import compose_transformations, reload_package, pose_to_matrix
from grasping.utils.avg_timer import Timer
from utils.logging import setup_logger
import tensorrt as trt
# https://github.com/NVIDIA/TensorRT/issues/1945
import torch
import pycuda.autoinit

from configs.grasping_config import Denoiser, ShapeCompletion, RANSAC, GraspDetection, Network, Logging
from grasping.grasp_detection.ransac_gd.trt.trt_ransac import TrTRansac, RansacTracker

setup_logger(**Logging.Logger.Params.to_dict())


class Grasping(Network.node):
    def __init__(self):
        super().__init__(**Network.Args.to_dict())
        self.seg_model = None
        self.denoiser = None
        self.pcr_encoder = None
        self.pcr_decoder = None
        self.ransac = None
        self.grasp_detector = None

        self.max_partial_points = 0

        self.prev_output = None

        self.timer = Timer(window=10)

        # self.watch = Watch()

    def startup(self):
        self.denoiser = Denoiser.model(**Denoiser.Args.to_dict())
        self.pcr_encoder = ShapeCompletion.Encoder.model(**ShapeCompletion.Encoder.Args.to_dict())
        self.pcr_decoder = ShapeCompletion.Decoder.model(**ShapeCompletion.Decoder.Args.to_dict())
        self.ransac = TrTRansac(RANSAC.Args.engine_path)
        self.partial_rs_tracker = RansacTracker(**RANSAC.Tracker.to_dict(), debug=True)
        self.grasp_detector = GraspDetection.model()

    def loop(self, data):
        output = data

        self.timer.start()
        # Input
        segmented_pc = data['segmented_pc']
        if segmented_pc in Signals:
            return {}

        # Add previous to output # TODO MAKE IT BETTER
        if data['point'] not in Signals:
            output['point'] = data['point']
        if data['obj_distance'] not in Signals:  # TODO make it better
            output['obj_distance'] = data['obj_distance']  # TODO make it better

        # Blocking should be fine as the camera pose streamer is much faster than this module
        if 'camera_pose' in data:
            camera_pose = data['camera_pose']
            camera_pose = pose_to_matrix(camera_pose)

        # Setup transformations
        R = Rotation.from_euler('xyz', [180, 0, 0], degrees=True).as_matrix()
        flip_z = np.array([[1, 0, 0], [0, 1, 0], [0, 0, -1]])

        # Downsample
        idx = np.random.choice(segmented_pc.shape[0], 4096, replace=False)
        downsampled_pc = segmented_pc[idx]

        logger.info("Point cloud downsampled", recurring=True)

        # Denoise
        denoised_pc = self.denoiser(downsampled_pc)

        logger.info("Partial point cloud denoised", recurring=True)

        # Fix Size
        if denoised_pc.shape[0] > 2024:
            idx = np.random.choice(denoised_pc.shape[0], 2024, replace=False)
            size_pc = denoised_pc[idx]
        else:
            logger.warning('Info: Partial Point Cloud padded', recurring=True)
            diff = 2024 - denoised_pc.shape[0]
            pad = np.zeros([diff, 3])
            pad[:] = segmented_pc[0]
            size_pc = np.vstack((denoised_pc, pad))
            
        # Count planes
        partial_box = self.ransac(size_pc, RANSAC.Args.tolerance, RANSAC.Args.iterations, num_planes=2)
        if partial_box is None:
            logger.warning('Ransac couldn\'t detect more than 1 plane in the input point cloud', recurring=True)
            output['fps_od'] = 1 / self.timer.compute(stop=True)
            self.prev_output = copy.deepcopy(output)
            return output
        
        _, aux = self.partial_rs_tracker(partial_box, size_pc)
        # If they are different it means we are using the old one (the inputs are quite similar)
        #   so we don't recompute everything and just return the old outputs
        if not np.all(aux == size_pc):
            logger.info('Using previous iteration output', recurring=True)
            output = self.prev_output
            output['rgb'] = data['rgb']
            output['depth'] = data['depth']
            output['fps_od'] = 1 / self.timer.compute(stop=True)
            return output

        # Normalize
        mean = np.mean(size_pc, axis=0)
        var = np.sqrt(np.max(np.sum((size_pc - mean) ** 2, axis=1)))
        
        
        # if self.old_mean is not None:
        #     print(np.linalg.norm(mean - self.old_mean))
        #     print(np.linalg.norm(var - self.old_var))
        #     if np.linalg.norm(mean - self.old_mean) < 0.05:
        #         mean = self.old_mean
        #     if np.linalg.norm(var - self.old_var) < 0.05:
        #         var = self.old_var
            
        # self.old_mean = mean
        # self.old_var = var
        
        normalized_pc = (size_pc - mean) / (var * 2)
        normalized_pc[..., -1] = -normalized_pc[..., -1]

        self.prev_denormalize = compose_transformations([flip_z, np.eye(3) * (var * 2), mean[np.newaxis]])
        denormalize = compose_transformations([self.prev_denormalize, R])

        # Reconstruction
        fast_weights = self.pcr_encoder(normalized_pc)
        reconstruction = self.pcr_decoder(fast_weights)

        logger.info("Computed object reconstruction", recurring=True)

        if reconstruction.shape[0] >= 15_000:
            logger.warning('Corrupted reconstruction - check the input point cloud', recurring=True)

            output['fps_od'] = 1 / self.timer.compute(stop=True)
            return output
        
        output['reconstruction'] = reconstruction

        center = np.mean(
            (np.block(
                [reconstruction, np.ones([reconstruction.shape[0], 1])]) @ denormalize)[..., :3], axis=0
        )[None]

        if Logging.debug:
            output['center'] = center

        try:
            box = self.ransac(reconstruction @ flip_z, RANSAC.Args.tolerance, 
                              RANSAC.Args.iterations, num_planes=6)
            # box, _ = self.rs_tracker(box, reconstruction @ flip_z)
            poses = self.grasp_detector(box)
        except ValueError as e:
            poses = None
            logger.warning(repr(e))

        logger.info("Hand poses computed", recurring=True)

        if poses is None:
            logger.warning('Corrupted reconstruction - check the input point cloud', recurring=True)
            output['fps_od'] = 1 / self.timer.compute(stop=True)
            self.prev_output = copy.deepcopy(output)
            return output

        hands_camera_frame = np.stack([compose_transformations([poses[1].T, poses[0][np.newaxis] * (var * 2) + mean, R]),
                          compose_transformations([poses[3].T, poses[2][np.newaxis] * (var * 2) + mean, R])], axis=-1)

        output['hands'] = hands_camera_frame

        if 'camera_pose' in data:
            hands_root_frame = np.stack([camera_pose @ compose_transformations([poses[1].T, poses[0][np.newaxis] * (var * 2) + mean, R]),
                                         camera_pose @ compose_transformations([poses[3].T, poses[2][np.newaxis] * (var * 2) + mean, R])], axis=-1)
            output['hands_root_frame'] = hands_root_frame
        # hands_normalized = np.stack([compose_transformations([poses[1].T, poses[0][np.newaxis]]),
        #                              compose_transformations([poses[3].T, poses[2][np.newaxis]])], axis=-1)

        if Logging.debug:
            output['planes'] = poses[4]
            output['lines'] = poses[5]
            output['vertices'] = poses[6] * (var * 2) + mean  # de-normalized
            output['partial'] = normalized_pc
            output['transform'] = denormalize

        output['fps_od'] = 1 / self.timer.compute(stop=True)
        self.prev_output = copy.deepcopy(output)
        return output


if __name__ == '__main__':
    grasping = Grasping()
    grasping.run()
