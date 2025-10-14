import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taxprotest.settings')
django.setup()

from data.tasks import download_and_extract_hcad


def main():
    print('Running download task synchronously (this will perform HTTP downloads in this process)')
    res = download_and_extract_hcad.apply().get()
    print('Result:', res)


if __name__ == '__main__':
    main()
