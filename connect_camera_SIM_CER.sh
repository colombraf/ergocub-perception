yarp connect /SIM_CER_ROBOT/depthCamera/rgbImage:o /depthCamera/rgbImage:r mjpeg
yarp connect /SIM_CER_ROBOT/depthCamera/depthImage:o /depthCamera/depthImage:r fast_tcp+send.portmonitor+file.depthimage_compression_zlib+recv.portmonitor+file.depthimage_compression_zlib+type.dll
