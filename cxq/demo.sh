#!/usr/bin/env bash
# CXQ V1 bake-off demo — runs all shared queries A–F and shows real output.
# Run from the libclang-lab directory: bash cxq/demo.sh

set -e
cd "$(dirname "$0")/.."   # ensure we're at libclang-lab root

echo "CXQ V1 Bake-off Demo"
echo "===================="
echo ""

# A. Attribute match
echo "=== A. Attribute match: function by name ==="
python3 -m cxq 'match function f where f.name ~ "^compute$" select f'
echo ""

echo "=== A2. Attribute match: functions with 'chain' in name ==="
python3 -m cxq 'match function f where f.name ~ "chain" select f'
echo ""

# B1. Inheritance relation
echo "=== B1. Relation: classes inheriting geo::Shape ==="
python3 -m cxq 'match class c where c inherits+ "geo::Shape" select c'
echo ""

# B2. Multi-match join
echo "=== B2. Join: class implementing an interface ==="
python3 -m cxq 'match class c, interface i where c inherits+ i select c, i'
echo ""

# C. Calls+ closure
echo "=== C. Closure: all functions reachable from compute via calls+ ==="
python3 -m cxq 'match function f where "compute" calls+ f select f'
echo ""

# D. Hierarchy closure
echo "=== D. Hierarchy closure: all descendants of geo::Shape ==="
python3 -m cxq 'match class c where c inherits+ "geo::Shape" select c'
echo ""

# E. Route (V1 cannot)
echo "=== E. Route query: call route from compute to leaf_a ==="
echo "V1 CANNOT express an ordered path/route query."
echo "V1 can only answer reachability (boolean set membership):"
python3 -m cxq 'match function f where f.name = "leaf_a" and "compute" calls+ f select f'
echo "(answer: yes, leaf_a IS reachable; but the route compute->mid->leaf_a is not returned)"
echo ""

# F. Ranking (V1 cannot)
echo "=== F. Ranking: top 10 functions by transitive-caller count ==="
echo "V1 CANNOT express ranking. No rank/order-by/limit/count(calls+) construct."
echo "Post-processing workaround: run the full match and sort in Python."
echo ""

echo "Full demo (with V1 explanation text): python3 cxq/demo.py"
