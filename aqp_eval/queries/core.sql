-- Q01
SELECT COUNT(*) AS cnt
FROM lineitem;

-- Q02
SELECT SUM(l_extendedprice) AS total_price
FROM lineitem;

-- Q03
SELECT AVG(l_discount) AS avg_discount
FROM lineitem;

-- Q04
SELECT SUM(l_extendedprice) AS total_price
FROM lineitem
WHERE l_discount > 0.08;

-- Q05
SELECT l_returnflag, COUNT(*) AS cnt
FROM lineitem
GROUP BY l_returnflag;

-- Q06
SELECT l_returnflag, SUM(l_extendedprice) AS total_price
FROM lineitem
GROUP BY l_returnflag;

-- Q07
SELECT l_returnflag, COUNT(*) AS cnt
FROM lineitem
WHERE l_discount > 0.05
GROUP BY l_returnflag;

-- Q08
SELECT l_suppkey, COUNT(*) AS cnt
FROM lineitem
GROUP BY l_suppkey;

-- Q09
SELECT
    c.c_mktsegment,
    COUNT(*) AS cnt
FROM customer c
JOIN orders o
    ON c.c_custkey = o.o_custkey
GROUP BY c.c_mktsegment;

-- Q10
SELECT
    c.c_mktsegment,
    SUM(o.o_totalprice) AS total_price
FROM customer c
JOIN orders o
    ON c.c_custkey = o.o_custkey
WHERE o.o_orderdate >= DATE '1995-01-01'
GROUP BY c.c_mktsegment;
