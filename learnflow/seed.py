import argparse
from learnflow.db import reset


def main():
    parser = argparse.ArgumentParser(description='Reset the local synthetic demo database.')
    parser.add_argument('--db', default='learnflow.sqlite3')
    args = parser.parse_args()
    reset(args.db)
    print(f'Reset and seeded {args.db}')


if __name__ == '__main__':
    main()
