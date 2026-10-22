from concurrent.futures import ThreadPoolExecutor
from Jobs.workday_alt import workday_alt_algor


WORKDAY_OUTER_WORKERS = 2


def split_into_chunks(items, num_chunks):
    # Round-robin so every chunk gets a similar mix of big and small slugs.
    return [items[i::num_chunks] for i in range(num_chunks) if items[i::num_chunks]]


def run_workday_chunk(slug_chunk):
    # Created inside the thread because SQLite connections are single-thread.
    workday_system = workday_alt_algor()
    workday_system.list_of_slugs = slug_chunk
    workday_system.data_aquasition()


def run_workday_parallel():
    # Registers every slug in the database before the workers start.
    setup = workday_alt_algor()
    setup.load_company_slugs()
    done_today = setup.database.get_slugs_scraped_today("Workday")
    pending = [slug for slug in setup.list_of_slugs if slug not in done_today]
    print(f"Skipping {len(setup.list_of_slugs) - len(pending)} slugs already finished today")

    chunks = split_into_chunks(pending, WORKDAY_OUTER_WORKERS)
    with ThreadPoolExecutor(max_workers=max(1, len(chunks))) as executor:
        for future in [executor.submit(run_workday_chunk, chunk) for chunk in chunks]:
            future.result()


def main():
    run_workday_parallel()


if __name__ == "__main__":
    main()
