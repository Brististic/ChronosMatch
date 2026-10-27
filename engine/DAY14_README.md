\# ChronosMatch — Day 14



\## Member 2: Cython matching engine



\### Test added

`tests/test\_gc\_stress.py` runs 20,000 sell/buy matching pairs while Python's cyclic garbage collector is disabled.



It checks that:

\- each buy order matches the expected sell order;

\- GC collection counters do not change during the workload;

\- the order book is empty and passes its integrity check afterward;

\- the test restores the GC state it started with.



\### Run from the `engine` directory



```cmd

python -m py\_compile tests\\test\_gc\_stress.py

python -m unittest tests.test\_gc\_stress -v

