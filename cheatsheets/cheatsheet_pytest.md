# Pytest Cheatsheet

pytest # run all tests in current dir
pytest test_file.py # run tests in a file
pytest -k "pattern" # run tests matching substring expr
pytest -m marker # run tests with specific marker
pytest -v # verbose output
pytest -q # quiet mode
pytest -x # stop after first failure
pytest --maxfail=3 # stop after 3 failures
pytest -s # disable output capture (print/logs show)
pytest --tb=short # shorter traceback
pytest --tb=line # single-line traceback
pytest --pdb # drop into debugger on failure
pytest --durations=5 # show 5 slowest tests
pytest --lf # run only last failed tests
pytest --ff # run failed tests first, then rest
pytest --cov=src # measure coverage for "src" dir
pytest --cov-report=html # generate HTML coverage report

# Markers

@pytest.mark.skip # skip this test
@pytest.mark.xfail # expected failure
@pytest.mark.parametrize("x,y", [(1,2),(3,4)]) # parametrize test

# Fixtures

@pytest.fixture # define reusable fixture
def myfix(): return 42
def test_x(myfix): assert myfix == 42

pytest --collect-only
