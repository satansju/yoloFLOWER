# OBSS SAHI Tool
# Code written by Fatih C Akyon, 2020.

import concurrent.futures
import logging
import os
import re
import shutil
import time
from email.mime import image
from pathlib import Path
from typing import Dict, List, Optional, Union, Tuple

import numpy as np
from slicing.coco import Coco, CocoAnnotation, CocoImage, create_coco_dict
from slicing.cv import read_image_as_pil
from slicing.file import load_json, save_json
from PIL import Image
from shapely.errors import TopologicalError
from tqdm import tqdm

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
    datefmt="%m/%d/%Y %H:%M:%S",
    level=os.environ.get("LOGLEVEL", "INFO").upper(),
)

MAX_WORKERS = 20


def get_slice_bboxes(
    image_height: int,
    image_width: int,
    slice_height: int = None,
    slice_width: int = None,
    auto_slice_resolution: bool = True,
    overlap_height_ratio: float = 0.2,
    overlap_width_ratio: float = 0.2,
) -> List[List[int]]:
    """Slices `image_pil` in crops.
    Corner values of each slice will be generated using the `slice_height`,
    `slice_width`, `overlap_height_ratio` and `overlap_width_ratio` arguments.
    Args:
        image_height (int): Height of the original image.
        image_width (int): Width of the original image.
        slice_height (int): Height of each slice. Default 512.
        slice_width (int): Width of each slice. Default 512.
        overlap_height_ratio(float): Fractional overlap in height of each
            slice (e.g. an overlap of 0.2 for a slice of size 100 yields an
            overlap of 20 pixels). Default 0.2.
        overlap_width_ratio(float): Fractional overlap in width of each
            slice (e.g. an overlap of 0.2 for a slice of size 100 yields an
            overlap of 20 pixels). Default 0.2.
        auto_slice_resolution (bool): if not set slice parameters such as slice_height and slice_width,
            it enables automatically calculate these params from image resolution and orientation.
    Returns:
        List[List[int]]: List of 4 corner coordinates for each N slices.
            [
                [slice_0_left, slice_0_top, slice_0_right, slice_0_bottom],
                ...
                [slice_N_left, slice_N_top, slice_N_right, slice_N_bottom]
            ]
    """
    slice_bboxes = []
    y_max = y_min = 0

    if slice_height and slice_width:
        y_overlap = int(overlap_height_ratio * slice_height)
        x_overlap = int(overlap_width_ratio * slice_width)
    elif auto_slice_resolution:
        x_overlap, y_overlap, slice_width, slice_height = get_auto_slice_params(height=image_height, width=image_width)
    else:
        raise ValueError("Compute type is not auto and slice width and height are not provided.")

    while y_max < image_height:
        x_min = x_max = 0
        y_max = y_min + slice_height
        while x_max < image_width:
            x_max = x_min + slice_width
            if y_max > image_height or x_max > image_width:
                xmax = min(image_width, x_max)
                ymax = min(image_height, y_max)
                xmin = max(0, xmax - slice_width)
                ymin = max(0, ymax - slice_height)
                slice_bboxes.append([xmin, ymin, xmax, ymax])
            else:
                slice_bboxes.append([x_min, y_min, x_max, y_max])
            x_min = x_max - x_overlap
        y_min = y_max - y_overlap
    return slice_bboxes


def annotation_inside_slice(annotation: Dict, slice_bbox: List[int]) -> bool:
    """Check whether annotation coordinates lie inside slice coordinates.
    Args:
        annotation (dict): Single annotation entry in COCO format.
        slice_bbox (List[int]): Generated from `get_slice_bboxes`.
            Format for each slice bbox: [x_min, y_min, x_max, y_max].
    Returns:
        (bool): True if any annotation coordinate lies inside slice.
    """
    left, top, width, height = annotation["bbox"]

    right = left + width
    bottom = top + height

    if left >= slice_bbox[2]:
        return False
    if top >= slice_bbox[3]:
        return False
    if right <= slice_bbox[0]:
        return False
    if bottom <= slice_bbox[1]:
        return False

    return True


def process_coco_annotations(coco_annotation_list: List[CocoAnnotation], slice_bbox: List[int], min_area_ratio) -> bool:
    """Slices and filters given list of CocoAnnotation objects with given
    'slice_bbox' and 'min_area_ratio'.
    Args:
        coco_annotation_list (List[CocoAnnotation])
        slice_bbox (List[int]): Generated from `get_slice_bboxes`.
            Format for each slice bbox: [x_min, y_min, x_max, y_max].
        min_area_ratio (float): If the cropped annotation area to original
            annotation ratio is smaller than this value, the annotation is
            filtered out. Default 0.1.
    Returns:
        (List[CocoAnnotation]): Sliced annotations.
    """

    sliced_coco_annotation_list: List[CocoAnnotation] = []
    for coco_annotation in coco_annotation_list:
        if annotation_inside_slice(coco_annotation.json, slice_bbox):
            sliced_coco_annotation = coco_annotation.get_sliced_coco_annotation(slice_bbox)
            if sliced_coco_annotation.area / coco_annotation.area >= min_area_ratio:
                sliced_coco_annotation_list.append(sliced_coco_annotation)
    return sliced_coco_annotation_list


class SlicedImage:
    def __init__(self, image, annotation, starting_pixel):
        """
        image: np.array
            Sliced image.
        coco_image: CocoImage
            Coco styled image object that belong to sliced image.
        starting_pixel: list of list of int
            Starting pixel coordinates of the sliced image.
        """
        self.image = image
        self.annotation = annotation
        self.starting_pixel = starting_pixel


class SliceImageResult:
    def __init__(self, original_image_size=None, image_dir: str = None):
        """
        sliced_image_list: list of SlicedImage
        image_dir: str
            Directory of the sliced image exports.
        original_image_size: list of int
            Size of the unsliced original image in [height, width]
        """
        self._sliced_image_list: List[SlicedImage] = []
        self.original_image_height = original_image_size[0]
        self.original_image_width = original_image_size[1]
        self.image_dir = image_dir

    def add_sliced_image(self, sliced_image: SlicedImage):
        if not isinstance(sliced_image, SlicedImage):
            raise TypeError("sliced_image must be a SlicedImage instance")

        self._sliced_image_list.append(sliced_image)

    @property
    def sliced_image_list(self):
        return self._sliced_image_list

    @property
    def images(self):
        """Returns sliced images.
        Returns:
            images: a list of np.array
        """
        images = []
        for sliced_image in self._sliced_image_list:
            images.append(sliced_image.image)
        return images
    
    @property
    def annotations(self):
        """Returns sliced annotations.
        Returns:
            annotations: a list of list of tuples
        """
        annotations = []
        for sliced_image in self._sliced_image_list:
            annotations.append(sliced_image.annotation)
        return annotations

    @property
    def coco_images(self) -> List[CocoImage]:
        """Returns CocoImage representation of SliceImageResult.
        Returns:
            coco_images: a list of CocoImage
        """
        coco_images: List = []
        for sliced_image in self._sliced_image_list:
            coco_images.append(sliced_image.coco_image)
        return coco_images

    @property
    def starting_pixels(self) -> List[int]:
        """Returns a list of starting pixels for each slice.
        Returns:
            starting_pixels: a list of starting pixel coords [x,y]
        """
        starting_pixels = []
        for sliced_image in self._sliced_image_list:
            starting_pixels.append(sliced_image.starting_pixel)
        return starting_pixels

    @property
    def filenames(self) -> List[int]:
        """Returns a list of filenames for each slice.
        Returns:
            filenames: a list of filenames as str
        """
        filenames = []
        for sliced_image in self._sliced_image_list:
            filenames.append(sliced_image.coco_image.file_name)
        return filenames

    def __len__(self):
        return len(self._sliced_image_list)

def read_yolo(path: str) -> List[Tuple]:
    if not os.path.exists(path):
        return []
    
    with open(path, "r") as f:
        lines = f.readlines()
    
    yolo = []
    for i in lines:
        values = re.sub("\n", "", "".join(i)).rsplit(" ")
        yolo.append(tuple(list(str(values[0])) + [float(i) for i in values[1:]]))

    return yolo

def yolo_to_coco(yolo: List[Tuple], width: int, height: int) -> List[CocoAnnotation]:
    """Convert a CocoAnnotation object to a pythonized YOLO annotation file.
    Args:
        yolo (list(tuple)): an object containing YOLO style annotations as tuples (lines) nested in a list.
    Returns:
        coco (CocoAnnotation): A coco style annotation object of class CocoAnnotation.     
    """
    def one_yolo_to_coco(yolo_bbox: tuple) -> dict:
        category_id, x_center, y_center, w, h = yolo_bbox
        xmin = (x_center - w/2) * width
        ymin = (y_center - h/2) * height
        w *= width
        h *= height
        coco_dict = {
            "bbox" : [xmin, ymin, w, h],
            "category_id" : category_id,
        }
        return CocoAnnotation.from_coco_annotation_dict(coco_dict)

    return [one_yolo_to_coco(i) for i in yolo]

def coco_to_yolo(coco: CocoAnnotation, width: int, height: int) -> tuple:
    """Convert a CocoAnnotation object to a pythonized YOLO annotation file.
    Args:
        coco (CocoAnnotation): a coco annotation object of class CocoAnnotation
    Returns:
        yolo: a list of each line that would be written in a YOLO style annotation file "class_id center_x center_y width height". 
             Each line is stored as a tuple of 5 elements containing each value in the line.     
    """
    x_center = coco.bbox[0] + coco.bbox[2] / 2.0
    y_center = coco.bbox[1] + coco.bbox[3] / 2.0
    bbox_width = coco.bbox[2]
    bbox_height = coco.bbox[3]
    x_center = x_center / width
    y_center = y_center / height
    bbox_width = bbox_width / width
    bbox_height = bbox_height / height
    category_id = coco.category_id
    
    return (str(category_id), x_center, y_center, bbox_width, bbox_height)

def slice_image(
    image: Union[str, Image.Image],
    yolo_annotation: Optional[str] = None,
    output_file_name: Optional[str] = None,
    output_dir: Optional[str] = None,
    slice_height: int = None,
    slice_width: int = None,
    overlap_height_ratio: float = None,
    overlap_width_ratio: float = None,
    auto_slice_resolution: bool = True,
    min_area_ratio: float = 0.1,
    out_ext: Optional[str] = ".jpg",
    min_out_slice_annotations: Optional[int] = None,
    verbose: bool = False,
) -> SliceImageResult:
    """Slice a large image into smaller windows. If output_file_name is given export
    sliced images.
    Args:
        image (str or PIL.Image): File path of image or Pillow Image to be sliced.
        yolo_annotation (List, optional): YOLOv5 annotation file paths as string.
        output_file_name (str, optional): Root name of output files (coordinates will
            be appended to this)
        output_dir (str, optional): Output directory
        slice_height (int): Height of each slice. Default 512.
        slice_width (int): Width of each slice. Default 512.
        overlap_height_ratio (float): Fractional overlap in height of each
            slice (e.g. an overlap of 0.2 for a slice of size 100 yields an
            overlap of 20 pixels). Default 0.2.
        overlap_width_ratio (float): Fractional overlap in width of each
            slice (e.g. an overlap of 0.2 for a slice of size 100 yields an
            overlap of 20 pixels). Default 0.2.
        auto_slice_resolution (bool): if not set slice parameters such as slice_height and slice_width,
            it enables automatically calculate these params from image resolution and orientation.
        min_area_ratio (float): If the cropped annotation area to original annotation
            ratio is smaller than this value, the annotation is filtered out. Default 0.1.
        out_ext (str, optional): Extension of saved images. Default is the
            original suffix.
        min_out_slice_annotations (int, optional): Minimum number of output annotations before a slice is pruned from export.
        verbose (bool, optional): Switch to print relevant values to screen.
            Default 'False'.
    Returns:
        sliced_image_result: SliceImageResult:
                                sliced_image_list: list of SlicedImage
                                image_dir: str
                                    Directory of the sliced image exports.
                                original_image_size: list of int
                                    Size of the unsliced original image in [height, width]
        num_total_invalid_segmentation: int
            Number of invalid segmentation annotations.
    """

    # define verboseprint
    verboselog = logger.info if verbose else lambda *a, **k: None

    # create outdir if not present
    if output_dir is not None:
        # shutil.rmtree(output_dir)
        image_dst = re.sub("\*", "images", output_dir)
        label_dst = re.sub("\*", "labels", output_dir)
        Path(image_dst).mkdir(parents=True, exist_ok=True)
        if yolo_annotation is not None:
            Path(label_dst).mkdir(parents=True, exist_ok=True)

    def export_single_slice(image: np.ndarray, annotation: Optional[List[Tuple]], slice_file_name: str):
        if annotation is not None and len(annotation) < min_out_slice_annotations:
            return
        slice_file_name_base = re.sub(f"{out_ext}$", "", slice_file_name)
        ## Export image
        image_pil = read_image_as_pil(image)
        image_slice_file_path = f'{image_dst}{os.sep}{slice_file_name_base}.jpg'
        # export sliced image
        if verbose:
            print("Attempting to save image at", image_slice_file_path)
        image_pil.save(image_slice_file_path)
        image_pil.close()  # to fix https://github.com/obss/sahi/issues/565
        verboselog("sliced image path: " + image_slice_file_path)
        ## Export annotation
        if annotation is not None:
            annotation_slice_file_path = f'{label_dst}{os.sep}{slice_file_name_base}.txt'
            if verbose:
                print("Attempting to save annotation at", annotation_slice_file_path)
            with open(annotation_slice_file_path, "x") as f:
                lines = [" ".join((str(j) for j in i)) for i in annotation]
                lines[:-1] = [i + "\n" for i in lines[:-1]]
                f.writelines(lines)
            verboselog("sliced annotation path: " + annotation_slice_file_path)

    # read image
    image_pil = read_image_as_pil(image)
    verboselog("image.shape: " + str(image_pil.size))

    image_width, image_height = image_pil.size
    if not (image_width != 0 and image_height != 0):
        raise RuntimeError(f"invalid image size: {image_pil.size} for 'slice_image'.")
    slice_bboxes = get_slice_bboxes(
        image_height=image_height,
        image_width=image_width,
        auto_slice_resolution=auto_slice_resolution,
        slice_height=slice_height,
        slice_width=slice_width,
        overlap_height_ratio=overlap_height_ratio,
        overlap_width_ratio=overlap_width_ratio,
    )

    if yolo_annotation is not None:
        annotations = read_yolo(yolo_annotation)
    else:
        annotations = []
    # print(annotations)

    if output_file_name and output_dir and len(annotations) < min_out_slice_annotations:
        if verbose:
            print("Too few bounding boxes in original image for any slices to possibly be exported! (" + yolo_annotation + ")")
        return None

    n_ims = 0

    # init images and annotations lists
    sliced_image_result = SliceImageResult(original_image_size=[image_height, image_width], image_dir=output_dir)
    slice_file_names = []

    image_pil_arr = np.asarray(image_pil)
    # iterate over slices
    for slice_bbox in slice_bboxes:
        n_ims += 1

        # extract image
        tlx = slice_bbox[0]
        tly = slice_bbox[1]
        brx = slice_bbox[2]
        bry = slice_bbox[3]
        image_pil_slice = image_pil_arr[tly:bry, tlx:brx]

        # process annotations if coco_annotations is given
        if yolo_annotation is not None:
            coco_annotation_list = yolo_to_coco(annotations, image_width, image_height)
            sliced_coco_annotation_list = process_coco_annotations(coco_annotation_list, slice_bbox, min_area_ratio)
            sliced_yolo_annotation_list = [coco_to_yolo(i, slice_width, slice_height) for i in sliced_coco_annotation_list]
        else:
            sliced_yolo_annotation_list = []

        # set image file suffixes
        slice_suffixes = "_".join(map(str, slice_bbox))
        if out_ext:
            suffix = out_ext
        else:
            try:
                suffix = Path(image_pil.filename).suffix
            except AttributeError:
                suffix = ".jpg"

        # set image file name and path
        slice_file_name = f"{output_file_name}_{slice_suffixes}{suffix}"
        slice_file_names.append(slice_file_name)

        # create coco image
        slice_width = slice_bbox[2] - slice_bbox[0]
        slice_height = slice_bbox[3] - slice_bbox[1]

        # # append coco annotations (if present) to coco image
        # if yolo_annotation is not None and (output_dir is not None or output_file_name is not None):
        #     raise Exception("Exporting yolo annotation list has not been implemented yet!")
        #     # for yolo_annotation in sliced_yolo_annotation_list:
        #         # write yolo_annotation

        # create sliced image and append to sliced_image_result
        sliced_image = SlicedImage(
            image=image_pil_slice, annotation=sliced_yolo_annotation_list, starting_pixel=[slice_bbox[0], slice_bbox[1]]
        )
        sliced_image_result.add_sliced_image(sliced_image)

    # export slices if output directory is provided
    if output_file_name and output_dir:
        for img, ann, file in zip(
            sliced_image_result.images,
            sliced_image_result.annotations if yolo_annotation is not None else None,
            slice_file_names):
            
            export_single_slice(img, ann, file)
        # conc_exec = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        # conc_exec.map(
        #     export_single_slice,
        #     sliced_image_result.images,
        #     sliced_image_result.annotations if yolo_annotation is not None else None,
        #     [output_dir] * len(sliced_image_result),
        #     slice_file_names
        # )

    verboselog(
        "Num slices: " + str(n_ims) + " slice_height: " + str(slice_height) + " slice_width: " + str(slice_width)
    )

    return sliced_image_result


# def slice_coco(
#     coco_annotation_file_path: str,
#     image_dir: str,
#     output_coco_annotation_file_name: str,
#     output_dir: Optional[str] = None,
#     ignore_negative_samples: bool = False,
#     slice_height: int = 512,
#     slice_width: int = 512,
#     overlap_height_ratio: float = 0.2,
#     overlap_width_ratio: float = 0.2,
#     min_area_ratio: float = 0.1,
#     out_ext: Optional[str] = None,
#     verbose: bool = False,
# ) -> List[Union[Dict, str]]:
#     """
#     Slice large images given in a directory, into smaller windows. If out_name is given export sliced images and coco file.
#     Args:
#         coco_annotation_file_pat (str): Location of the coco annotation file
#         image_dir (str): Base directory for the images
#         output_coco_annotation_file_name (str): File name of the exported coco
#             datatset json.
#         output_dir (str, optional): Output directory
#         ignore_negative_samples (bool): If True, images without annotations
#             are ignored. Defaults to False.
#         slice_height (int): Height of each slice. Default 512.
#         slice_width (int): Width of each slice. Default 512.
#         overlap_height_ratio (float): Fractional overlap in height of each
#             slice (e.g. an overlap of 0.2 for a slice of size 100 yields an
#             overlap of 20 pixels). Default 0.2.
#         overlap_width_ratio (float): Fractional overlap in width of each
#             slice (e.g. an overlap of 0.2 for a slice of size 100 yields an
#             overlap of 20 pixels). Default 0.2.
#         min_area_ratio (float): If the cropped annotation area to original annotation
#             ratio is smaller than this value, the annotation is filtered out. Default 0.1.
#         out_ext (str, optional): Extension of saved images. Default is the
#             original suffix.
#         verbose (bool, optional): Switch to print relevant values to screen.
#             Default 'False'.
#     Returns:
#         coco_dict: dict
#             COCO dict for sliced images and annotations
#         save_path: str
#             Path to the saved coco file
#     """

#     # read coco file
#     coco_dict: Dict = load_json(coco_annotation_file_path)
#     # create image_id_to_annotation_list mapping
#     coco = Coco.from_coco_dict_or_path(coco_dict)
#     # init sliced coco_utils.CocoImage list
#     sliced_coco_images: List = []

#     # iterate over images and slice
#     for coco_image in tqdm(coco.images):
#         # get image path
#         image_path: str = os.path.join(image_dir, coco_image.file_name)
#         # get annotation json list corresponding to selected coco image
#         # slice image
#         try:
#             slice_image_result = slice_image(
#                 image=image_path,
#                 coco_annotation_list=coco_image.annotations,
#                 output_file_name=Path(coco_image.file_name).stem,
#                 output_dir=output_dir,
#                 slice_height=slice_height,
#                 slice_width=slice_width,
#                 overlap_height_ratio=overlap_height_ratio,
#                 overlap_width_ratio=overlap_width_ratio,
#                 min_area_ratio=min_area_ratio,
#                 out_ext=out_ext,
#                 verbose=verbose,
#             )
#             # append slice outputs
#             sliced_coco_images.extend(slice_image_result.coco_images)
#         except TopologicalError:
#             logger.warning(f"Invalid annotation found, skipping this image: {image_path}")

#     # create and save coco dict
#     coco_dict = create_coco_dict(
#         sliced_coco_images, coco_dict["categories"], ignore_negative_samples=ignore_negative_samples
#     )
#     save_path = ""
#     if output_coco_annotation_file_name and output_dir:
#         save_path = Path(output_dir) / (output_coco_annotation_file_name + "_coco.json")
#         save_json(coco_dict, save_path)

#     return coco_dict, save_path


def calc_ratio_and_slice(orientation, slide=1, ratio=0.1):
    """
    According to image resolution calculation overlap params
    Args:
        orientation: image capture angle
        slide: sliding window
        ratio: buffer value
    Returns:
        overlap params
    """
    if orientation == "vertical":
        slice_row, slice_col, overlap_height_ratio, overlap_width_ratio = slide, slide * 2, ratio, ratio
    elif orientation == "horizontal":
        slice_row, slice_col, overlap_height_ratio, overlap_width_ratio = slide * 2, slide, ratio, ratio
    elif orientation == "square":
        slice_row, slice_col, overlap_height_ratio, overlap_width_ratio = slide, slide, ratio, ratio

    return slice_row, slice_col, overlap_height_ratio, overlap_width_ratio  # noqa


def calc_resolution_factor(resolution: int) -> int:
    """
    According to image resolution calculate power(2,n) and return the closest smaller `n`.
    Args:
        resolution: the width and height of the image multiplied. such as 1024x720 = 737280
    Returns:
    """
    expo = 0
    while np.power(2, expo) < resolution:
        expo += 1

    return expo - 1


def calc_aspect_ratio_orientation(width: int, height: int) -> str:
    """
    Args:
        width:
        height:
    Returns:
        image capture orientation
    """

    if width < height:
        return "vertical"
    elif width > height:
        return "horizontal"
    else:
        return "square"


def calc_slice_and_overlap_params(resolution: str, height: int, width: int, orientation: str) -> List:
    """
    This function calculate according to image resolution slice and overlap params.
    Args:
        resolution: str
        height: int
        width: int
        orientation: str
    Returns:
        x_overlap, y_overlap, slice_width, slice_height
    """

    if resolution == "medium":
        split_row, split_col, overlap_height_ratio, overlap_width_ratio = calc_ratio_and_slice(
            orientation, slide=1, ratio=0.8
        )

    elif resolution == "high":
        split_row, split_col, overlap_height_ratio, overlap_width_ratio = calc_ratio_and_slice(
            orientation, slide=2, ratio=0.4
        )

    elif resolution == "ultra-high":
        split_row, split_col, overlap_height_ratio, overlap_width_ratio = calc_ratio_and_slice(
            orientation, slide=4, ratio=0.4
        )
    else:  # low condition
        split_col = 1
        split_row = 1
        overlap_width_ratio = 1
        overlap_height_ratio = 1

    slice_height = height // split_col
    slice_width = width // split_row

    x_overlap = int(slice_width * overlap_width_ratio)
    y_overlap = int(slice_height * overlap_height_ratio)

    return x_overlap, y_overlap, slice_width, slice_height  # noqa


def get_resolution_selector(res: str, height: int, width: int):
    """
    Args:
        res: resolution of image such as low, medium
        height:
        width:
    Returns:
        trigger slicing params function and return overlap params
    """
    orientation = calc_aspect_ratio_orientation(width=width, height=height)
    x_overlap, y_overlap, slice_width, slice_height = calc_slice_and_overlap_params(
        resolution=res, height=height, width=width, orientation=orientation
    )

    return x_overlap, y_overlap, slice_width, slice_height


def get_auto_slice_params(height: int, width: int):
    """
    According to Image HxW calculate overlap sliding window and buffer params
    factor is the power value of 2 closest to the image resolution.
        factor <= 18: low resolution image such as 300x300, 640x640
        18 < factor <= 21: medium resolution image such as 1024x1024, 1336x960
        21 < factor <= 24: high resolution image such as 2048x2048, 2048x4096, 4096x4096
        factor > 24: ultra-high resolution image such as 6380x6380, 4096x8192
    Args:
        height:
        width:
    Returns:
        slicing overlap params x_overlap, y_overlap, slice_width, slice_height
    """
    resolution = height * width
    factor = calc_resolution_factor(resolution)
    if factor <= 18:
        return get_resolution_selector("low", height=height, width=width)
    elif 18 <= factor < 21:
        return get_resolution_selector("medium", height=height, width=width)
    elif 21 <= factor < 24:
        return get_resolution_selector("high", height=height, width=width)
    else:
        return get_resolution_selector("ultra-high", height=height, width=width)
