import os
import glob
import re
import pandas
import multiprocessing.dummy as mpd
from tqdm import tqdm
from typing import List, Tuple, Dict

def bboxesFromDirectory(dir: str) -> pandas.DataFrame:
    ###
    # Arguments:
    #   dir: path to the directory containing the metadata files (assumes they are csv files with columns 'file', 'region_attributes' (class name) and 'region_shape_attributes' (bounding box coordinates))
    # return: pandas.DataFrame with a row for each bounding box and three columns: 
    #   'path' contains the file name of the image which the bounding box belongs to, 
    #   'class' class label of the bounding box & 
    #   'bbox' a list of length 4 which contains [xmax, ymax, width, height] as integer pixel coordinates  
    ###
    meta_data_paths = []
    img_paths = []

    for i in glob.iglob(dir + os.sep + "**", recursive = True):
        i = re.sub(f'[\/\\\\]+', "/", i)
        if re.search("\.csv$", i):
            meta_data_paths.append(i)
        if re.search("\.JPG|\.jpg|\.jpeg|\.png", i):
            img_paths.append(i)

    combinedBBoxes = pandas.DataFrame({"path": [],"class" : [], "bbox" : []})

    for path in meta_data_paths:
        thisCSV = pandas.read_csv(path)

        thisClasses = [re.findall("(?<=stage\"\:\")[a-zA-Z]+|(?<=state\"\:\")[a-zA-Z]+", i) for i in thisCSV["region_attributes"]]

        thisBBoxes = [[int(j) for j in re.findall("[0-9]+", i)] for i in thisCSV["region_shape_attributes"]]

        thisCSV = pandas.DataFrame({
            "path" : thisCSV["filename"],
            "class" : thisClasses,
            "bbox" : thisBBoxes
        })
        
        combinedBBoxes = pandas.concat([combinedBBoxes, thisCSV])
    
    return combinedBBoxes

class class_to_index:
    def __init__(self, excluded_classes: List[str] = ["Gone"]):
        possible_classes = ["Bud", "Flower", "Withered", "Immature", "Mature", "Gone"]
        self.dict = {c : i for i, c in enumerate([c for c in possible_classes if c not in excluded_classes])}
        self.inv = {v : k for k, v in self.dict.items()}

    def translate(self, cls: str or list[str]) -> int or list[int]:
        if len(cls) == 1 or type(cls) == str:
            return self.dict[cls]
        elif len(cls) > 1:
            return [self.dict[i] for i in cls]
        else:
            raise ValueError("cls must be a string or a non-empty list of strings.")

    def invert(self, ind: int or list[int]) -> str or list[str]:
        if len(ind) == 1 or type(ind) == int:
            return self.inv[ind]
        elif len(ind) > 1:
            return [self.inv[i] for i in ind]
        else:
            raise ValueError("ind must be a string or a non-empty list of strings.")

def clean_filename(filename: str, with_dir: bool = False, only_file: bool = False, with_ext: bool = True):
    ###
    # Arguments:
    #   filename: an image file name/path
    #   with_dir: should the returned string keep the path directories?
    #   only_file: should the returned string only contain the file name?
    #   with_ext: should the returned string keep the file extension? 
    # return: A string that uniquely identifies and matches both the image file name of the actual image file and the image file name in the metadata files 
    ###
    if not with_ext:
        filename = re.sub("\.[a-zA-Z]+$", "", filename)

    if with_dir:
        parts = filename.rsplit(os.sep)
        filename = parts[-1]
        dir = os.sep.join(parts[:-1])

    filetype = re.search("\.[a-zA-Z]+$", filename)
    if filetype is not None:
        filetype = filetype.group(0)
    else:
        filetype = ""
    filename = re.sub("^0", "", filename)
    filename = re.search("^B[0-9X_-]+|^NARS[0-9X_-]+|^BJOR[0-9X_-]+", filename).group(0)
    filename = re.sub("^B(?=[0-9])", "BJOR_", filename)
    filename = re.sub("\-","_", filename)
    filename = re.sub("_$", "", filename)
    if only_file:
        return filename + filetype
    if not with_dir:
        return re.sub("X", "", filename) + filetype
    else:
        return dir + os.sep + filename + filetype

def get_series(path):
    ###
    # Arguments:
    #   path: path of image file subdirectory
    # return: A unique and clean subdirectory name
    ###
    init = re.search("NARS_[0-9]+|BJOR_[0-9]+|B[0-9]+", path).group(0)
    return re.sub("^B(?=[0-9])","BJOR_", init)

def make_series_dir(path, dir):
  series = get_series(clean_filename(path))
  if not os.path.exists(f'{dir}{os.sep}images{os.sep}{series}{os.sep}'):
      os.makedirs(f'{dir}{os.sep}images{os.sep}{series}{os.sep}')
      os.makedirs(f'{dir}{os.sep}labels{os.sep}{series}{os.sep}')

class clean_filenames:
    def __init__(self, dir: str):
        self.dict = {clean_filename(i, True, True, False) : i for i in glob.iglob(f'{dir}{os.sep}**{os.sep}**')}
        self.inv = {v : k for k, v in self.dict.items()}

    def translate(self, file: str or list[str]) -> str or list[str]:
        if len(file) == 1 or type(file) == str:
            return self.dict[file]
        elif len(file) > 1:
            return [self.dict[i] for i in file]
        else:
            raise ValueError("File must be a string or list of strings.")

    def invert(self, file: str or list[str]) -> str or list[str]:
        if len(file) == 1 or type(file) == str:
            return self.inv[file]
        elif len(file) > 1:
            return [self.inv[i] for i in file]
        else:
            raise ValueError("File must be a string or list of strings.")

def create_yolo_annotations(dir: str, out_dir: str, verbose: bool = False, excluded_classes: List[str] or str = "Gone") -> None:
    if not isinstance(excluded_classes, list) and isinstance(excluded_classes, str):
        excluded_classes = [excluded_classes]
    
    file_cleaner = clean_filenames(dir)
    class_translator = class_to_index(excluded_classes=excluded_classes)
    annotations = bboxesFromDirectory(dir)

    if not os.path.exists(out_dir + os.sep + "images"):
        if verbose:
            print("Output directory does not contain any images!")
        os.makedirs(out_dir + os.sep + "images", exist_ok=True)
    if not os.path.exists(out_dir + os.sep + "labels"):
        os.makedirs(out_dir + os.sep + "labels", exist_ok=True)
        
    for i in os.listdir(out_dir + os.sep + "images"):
        sub = out_dir + os.sep + "labels" + os.sep + i
        if not os.path.exists(sub):
            os.makedirs(sub, exist_ok=True)

    image_names = []

    for i in glob.iglob(f'{out_dir}{os.sep}images{os.sep}**{os.sep}**'):
        if len(i) > 0:
            try:
                image_names.append(clean_filename(i, True, True, False))
            except:
                raise ValueError("Invalid image file name: " + i)

    def extract_data(src: str, data: pandas.DataFrame) -> None:
        newsrc = clean_filename(src, True, True, False)
        img_series = get_series(newsrc)

        if newsrc not in image_names:
            if verbose:
                print(newsrc + " could not find a matching image file! (" + src + ")")
            return 

        label_dst = f'{out_dir}{os.sep}labels{os.sep}{img_series}{os.sep}{newsrc}.txt'

        xscale, yscale = (6080, 3420)

        if os.path.exists(label_dst):
            open_type = "w"
        else:
            open_type = "x"

        with open(label_dst, open_type) as f:
            for cls, bbox in zip(data["class"], data["bbox"]):
                if not cls or cls[0] in excluded_classes:
                    continue
                cls = cls[0]
                xmax, ymax, width, height = tuple(bbox)
                xcenter = xmax + width/2
                ycenter = ymax + height/2
                xcenter /= xscale
                width /= xscale
                ycenter /= yscale
                height /= yscale

                f.write(f'{class_translator.translate(cls)} {round(xcenter, 7)} {round(ycenter, 7)} {round(width, 7)} {round(height, 7)}\n')

    for i in file_cleaner.dict.keys():
        make_series_dir(i, out_dir)

    pool = mpd.Pool()
    pool.starmap(extract_data, tqdm(annotations.groupby("path")))