import time
import random
import string
import cv2
from PIL import Image
from PIL.ExifTags import TAGS
from datetime import datetime
import subprocess
import os


def create_hardlink_for_image(file_path, target_dir, dest_file_list, delay=True):
    if file_path.lower().endswith(('.png', '.jpg', '.jpeg', '.heic')):
        source_file_path = file_path
        # Split the path into head (directory) and tail (file name)
        head, _ = os.path.split(file_path)
        # Split the head into parts
        parts = head.split(os.sep)
        # Join the middle parts
        relative_path = os.sep.join(parts[1:])
        target_sub_dir_1 = os.path.join(target_dir, relative_path)


        for dest_file_name in dest_file_list:
            if os.sep in dest_file_name:
                team_file = dest_file_name.split(os.sep)
                target_sub_dir = os.path.join(target_sub_dir_1, team_file[0])
                dest_file_name = team_file[1]
            else:
                target_sub_dir = target_sub_dir_1
            
            if not os.path.exists(target_sub_dir):
                os.makedirs(target_sub_dir)
            
            target_file_path = os.path.join(target_sub_dir, dest_file_name)
            try:
                if not os.path.exists(target_file_path):
                    # Use mklink command to create a hard link
                    subprocess.run(['mklink', '/H', target_file_path, source_file_path], check=True, shell=True)
                    #print(f'Hard link created: {target_file_path} -> {source_file_path}')
                    if delay: time.sleep(0.5)
            except subprocess.CalledProcessError as e:
                print(f'Error creating hard link for {source_file_path}: {e}')


def create_symlink_for_image(file_path, target_dir, dest_file_list):
    if file_path.lower().endswith(('.png', '.jpg', '.jpeg', '.heic')):
        source_file_path = file_path
        # Split the path into head (directory) and tail (file name)
        #head, _ = os.path.split(file_path)
        # Split the head into parts
        #parts = head.split(os.sep)
        # Join the middle parts
        #relative_path = os.sep.join(parts[:-1])
        current_folder = os.getcwd()
        target_sub_dir_1 = os.path.join(current_folder, *target_dir)

        for dest_file_name in dest_file_list:
            if os.sep in dest_file_name:
                team_file = dest_file_name.split(os.sep)
                target_sub_dir = os.path.join(target_sub_dir_1, team_file[0])
                dest_file_name = team_file[1]
            else:
                target_sub_dir = target_sub_dir_1
            
            if not os.path.exists(target_sub_dir):
                os.makedirs(target_sub_dir)
            
            target_file_path = os.path.join(target_sub_dir, dest_file_name)

            try:
                if not os.path.exists(target_file_path):
                    # Use os.symlink to create a symbolic link
                    os.symlink(source_file_path, target_file_path)
                    #print(f'Symbolic link created: {target_file_path} -> {source_file_path}')
            except OSError as e:
                print(f'Error creating symbolic link for {source_file_path}: {e}')


# def get_image_creation_date(pil_image):
#     exif_data = pil_image.getexif()
#     ret = None
#     if exif_data:
#         for tag, value in exif_data.items():
#             tag_name = TAGS.get(tag, tag)
#             if  tag_name == 'DateTime':
#                 # Convert the string to a datetime object using the correct format
#                 try:
#                     ret = datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
#                 except ValueError:
#                     return "Invalid date format in EXIF data"
#                 break
#     return ret

def get_image_creation_date(pil_image):
    exif_data = pil_image.getexif()
    ret = None
    if exif_data:
        # for key, value in TAGS.items():
        #     if value == "ExifOffset":
        #         break
        
        # 0x8769 is the tag for Exif IFD
        info = exif_data.get_ifd(0x8769)
        for key, value in info.items():
            tag_name = TAGS.get(key, key)
            if  tag_name == 'DateTimeOriginal':
                # Convert the string to a datetime object using the correct format
                try:
                    ret = datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
                except ValueError:
                    return "Invalid date format in EXIF data"
                break
    return ret

def generate_unique_filename(score=0.1, extension='.jpg'):
    timestamp = int(time.time())  # Get current timestamp
    # Use SystemRandom to generate a random string to prevent get the same value
    # Because random.seed() is not called, the random number is generated from the system entropy source
    system_random = random.SystemRandom()
    random_suffix = ''.join(system_random.choices(string.ascii_lowercase + string.digits, k=2))
    prefix = format_float(score)
    return f"{prefix}_{timestamp}{random_suffix}{extension}"

def format_float(number):
    if number <= 0 or number >= 1:
        raise ValueError("The number must be greater than 0 and less than 1.")
    
    # Round the number to 4 decimal places and convert to string
    rounded_number = f"{number:.4f}"
    
    # Remove the leading '0.' to get a 4-digit number
    four_digit_number = rounded_number[2:]
    
    # Pad with leading zeros to ensure the output length is 4 characters
    formatted_number = four_digit_number.zfill(4)
    
    return formatted_number


def cv2_to_pil(cv2_image):
    """
    Convert an OpenCV image to a PIL image.

    Parameters:
    cv2_image (numpy.ndarray): The OpenCV image to be converted.

    Returns:
    PIL.Image.Image: The converted PIL image.
    """
    # Convert the image from BGR to RGB format
    cv2_image_rgb = cv2.cvtColor(cv2_image, cv2.COLOR_BGR2RGB)
    # Convert the OpenCV image (NumPy array) to a PIL image
    pil_image = Image.fromarray(cv2_image_rgb)
    return pil_image


def resize_image_auto_width(pil_image, fixed_height=1200):


    # Get original dimensions
    original_width, original_height = pil_image.size
    #print(f"Original size: {pil_image.size}")
    
    # Calculate the new width maintaining the aspect ratio
    new_width = int((fixed_height / original_height) * original_width)
    new_size = (new_width, fixed_height)
    
    # Resize the image with high-quality resampling
    img_resized = pil_image.resize(new_size, Image.LANCZOS)

    return img_resized


if __name__ == '__main__':

    # Example usage and test
    test_numbers = [0.1234, 0.5, 0.007, 0.98765]

    for number in test_numbers:
        formatted_string = format_float(number)
        print(f"Input: {number} -> Formatted: {formatted_string}")

    image_path = 'q/DSC_8960.JPG'
    creation_date = get_image_creation_date(image_path)
    print(f'Creation Date: {creation_date}')

    # Example usage
    filename = generate_unique_filename('.jpg')
    print(filename)  # e.g., '1625678901a1b2.jpg'

    # Example usage:
    # Load an image using OpenCV
    cv2_image = cv2.imread('path/to/your/image.jpg')

    # Convert the OpenCV image to a PIL image
    pil_image = cv2_to_pil(cv2_image)

    # Display the PIL image (optional)
    pil_image.show()

    # Save the PIL image (optional)
    pil_image.save('path/to/save/pil_image.png')

    
    # Example usage
    source_file_path = 'path/to/your/source_image.jpg'
    target_directory = 'path/to/your/target_dir'

    # Create a symbolic link
    create_symlink_for_image(source_file_path, target_directory)
