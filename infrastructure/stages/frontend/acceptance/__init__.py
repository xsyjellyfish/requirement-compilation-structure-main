"""Independent black-box acceptance layer for the generated frontend.

The trigger suites of step 12 prove that one requirement's interaction reaches its API.
They are still written against the design (``data-testid`` first) and can seed Given rows
with SQL. This layer answers a different question: can an ordinary visitor, using only
what the browser shows, complete the business journey the requirement describes?

Modules:

``runtime``   compiler-owned Playwright config, runner and fixture (isolated browser
              context, read-only database access, no mocks, no display fixtures).
``journey``   deterministic CRUD journey plan derived from the API IR, the database
              schema, the UI design and the trigger design, plus the rendered spec.
``audit``     static rules that reject a suite which is not a black-box acceptance test.
``checks``    Playwright invocation and result classification.
``pipeline``  prepare -> generate -> audit -> run -> repair, shared by steps 12 and 13.
"""
