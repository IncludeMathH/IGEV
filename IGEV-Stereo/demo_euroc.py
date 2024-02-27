import sys
sys.path.append('core')
DEVICE = 'cuda'
import os
os.environ['CUDA_VISIBLE_DEVICES'] = '0'
import argparse
import glob
import numpy as np
import torch
from tqdm import tqdm
from pathlib import Path
from igev_stereo import IGEVStereo
from utils.utils import InputPadder
from PIL import Image
from matplotlib import pyplot as plt
import os
import cv2

import glob
import pandas as pd
import os

def get_reference_timestamps(csv_dir):
    # 获取所有CSV文件
    files = glob.glob(csv_dir)

    # 初始化一个空列表来保存所有的参考图像时间戳
    reference_timestamps = {}

    # 遍历所有的CSV文件
    for file in files:
        # 读取CSV文件
        data = pd.read_csv(file)
        # 添加参考图像时间戳到列表
        reference_timestamps[os.path.basename(file).split('.')[0]] = data['Reference Timestamp'].tolist()

    return reference_timestamps

def convert_filename(filename):
    # 分割文件名
    parts = filename.split('/')
    # 组合新的文件名
    new_filename = f"{parts[2]}-{parts[4]}-{parts[-1]}"
    return new_filename

def load_image(imfile):
    img = np.array(Image.open(imfile)).astype(np.uint8)
    if img.ndim == 2:
        img = np.stack([img, img, img], axis=2)
    img = torch.from_numpy(img).permute(2, 0, 1).float()
    return img[None].to(DEVICE)

def demo(args):
    model = torch.nn.DataParallel(IGEVStereo(args), device_ids=[0])
    model.load_state_dict(torch.load(args.restore_ckpt))

    model = model.module
    model.to(DEVICE)
    model.eval()

    output_directory = Path(args.output_directory)
    output_directory.mkdir(exist_ok=True)

    with torch.no_grad():
        timestamps = get_reference_timestamps(args.csv_dir)

        left_images = []
        right_images = []
        dataset_dir = args.dataset_dir
        for folder_name, stamps in timestamps.items():
            for stamp in stamps:
                left_img_dir = os.path.join(dataset_dir, folder_name, 'mav0/cam0/data', f'{stamp}.png')
                right_img_dir = left_img_dir.replace('cam0', 'cam1')
                left_images.append(left_img_dir)
                right_images.append(right_img_dir)

        print(f"Found {len(left_images)} images. Saving files to {output_directory}/")

        Path(os.path.join(args.output_directory, 'images')).mkdir(exist_ok=True, parents=True)
        Path(os.path.join(args.output_directory, 'depth')).mkdir(exist_ok=True, parents=True)

        for (imfile1, imfile2) in tqdm(list(zip(left_images, right_images))):
            image1 = load_image(imfile1)    # (1, 3, h, w)
            image2 = load_image(imfile2)
            if args.stereo_depth:
                image3 = image1.clone().flip(dims=[2, 3])
                image4 = image2.clone().flip(dims=[2, 3])

            padder = InputPadder(image1.shape, divis_by=32)
            if args.stereo_depth:
                image1, image2, image3, image4 = padder.pad(image1, image2, image3, image4)
            else:
                image1, image2 = padder.pad(image1, image2)

            disp = model(image1, image2, iters=args.valid_iters, test_mode=True)
            disp = disp.cpu().numpy()
            disp = padder.unpad(disp)
            file_stem = convert_filename(imfile1)
            if args.stereo_depth:
                file_stem_left, file_stem_right = convert_filename(imfile1), convert_filename(imfile2)
                disp_right = model(image4, image3, iters=args.valid_iters, test_mode=True).cpu().numpy()
                disp_right = padder.unpad(disp_right)[:, :, ::-1, ::-1]
                filename_left, filename_right = os.path.join(output_directory, f"{file_stem_left}.png"), os.path.join(output_directory, f"{file_stem_right}.png")

                plt.imsave(output_directory / 'images' / f"{file_stem_left}.png", disp.squeeze(), cmap='jet')
                np.save(output_directory / 'depth' / f"{file_stem_left}.npy", disp.squeeze())
                plt.imsave(output_directory / 'images' / f"{file_stem_right}.png", disp_right.squeeze(), cmap='jet')
                np.save(output_directory / 'depth' / f"{file_stem_right}.npy", disp_right.squeeze())
            else:
                filename = os.path.join(output_directory, f"{file_stem}.png")
                plt.imsave(output_directory / f"{file_stem}.png", disp.squeeze(), cmap='jet')
                np.save(output_directory / f"{file_stem}.npy", disp.squeeze())
                # disp = np.round(disp * 256).astype(np.uint16)
                # cv2.imwrite(filename, cv2.applyColorMap(cv2.convertScaleAbs(disp.squeeze(), alpha=0.01),cv2.COLORMAP_JET), [int(cv2.IMWRITE_PNG_COMPRESSION), 0])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--restore_ckpt', help="restore checkpoint", default='./pretrained_models/sceneflow/sceneflow.pth')
    parser.add_argument('--save_numpy', action='store_true', help='save output as numpy arrays')

    parser.add_argument('--csv_dir', help="path to csv file", default="data/sample_euroc/*.csv")
    parser.add_argument('--dataset_dir', help="path to dataset", default='data/EuRoC/')

    parser.add_argument('--output_directory', help="directory to save output", default="./demo-output/")
    parser.add_argument('--mixed_precision', action='store_true', help='use mixed precision')
    parser.add_argument('--valid_iters', type=int, default=32, help='number of flow-field updates during forward pass')

    # Architecture choices
    parser.add_argument('--hidden_dims', nargs='+', type=int, default=[128]*3, help="hidden state and context dimensions")
    parser.add_argument('--corr_implementation', choices=["reg", "alt", "reg_cuda", "alt_cuda"], default="reg", help="correlation volume implementation")
    parser.add_argument('--shared_backbone', action='store_true', help="use a single backbone for the context and feature encoders")
    parser.add_argument('--corr_levels', type=int, default=2, help="number of levels in the correlation pyramid")
    parser.add_argument('--corr_radius', type=int, default=4, help="width of the correlation pyramid")
    parser.add_argument('--n_downsample', type=int, default=2, help="resolution of the disparity field (1/2^K)")
    parser.add_argument('--slow_fast_gru', action='store_true', help="iterate the low-res GRUs more frequently")
    parser.add_argument('--n_gru_layers', type=int, default=3, help="number of hidden GRU levels")
    parser.add_argument('--max_disp', type=int, default=192, help="max disp of geometry encoding volume")
    parser.add_argument('--stereo_depth', action='store_true', help='output stereo depth map')
    
    args = parser.parse_args()

    # Path(args.output_directory).mkdir(exist_ok=True, parents=True)

    demo(args)
