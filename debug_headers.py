
import csv
import os

def inspect_headers(filepath):
    print(f"--- Inspecting {filepath} ---")
    try:
        if not os.path.exists(filepath):
             print(f"File not found: {filepath}")
             return

        with open(filepath, 'r', encoding='latin-1') as f:
            # Read first few lines
            for i in range(5):
                line = f.readline().strip()
                if i == 0:
                    print(f"Header: {line}")
                    # Try to detect delimiter
                    if "\t" in line:
                         delimiter = "\t"
                         print("Detected delimiter: TAB")
                    else:
                         delimiter = ","
                         print("Detected delimiter: COMMA")
                    
                    headers = line.split(delimiter)
                    print(f"Columns: {headers}")
                else:
                    print(f"Row {i}: {line}")
    except Exception as e:
        print(f"Error reading {filepath}: {e}")

base_dir = 'downloads/Real_building_land'
inspect_headers(f'{base_dir}/building_res.txt')
inspect_headers(f'{base_dir}/extra_features.txt')
