# Delta Lake Repartitioning Strategy

## Can You Change Partitioning on an Existing Table?

**Short Answer:** No, not inline. Partitioning is a **physical structure** decision made at table creation.

**Long Answer:** You have several options to repartition based on query history.

---

## Understanding the Problem

### What Partitioning Is:
```
warehouse/dbo/sales/
├── region=EMEA/
│   └── *.parquet
├── region=APAC/
│   └── *.parquet
└── region=NA/
    └── *.parquet
```

This physical directory structure **cannot be changed** without recreating the table.

### Why You'd Want to Repartition:

**Scenario:** You created a table partitioned by `created_date`, but after 6 months of queries, you realize 90% of queries filter by `region`, not date.

**Current reality:**
- Queries filter `WHERE region = 'EMEA'` → reads ALL partitions (slow)
- Partitioning by `created_date` → unused optimization

**Desired state:**
- Partition by `region` instead → queries read only 1 partition (fast)

---

## Repartitioning Options

### Option 1: CREATE TABLE AS SELECT (CTAS) with New Partitions

**Best for:** Production repartitioning with minimal downtime

```sql
-- 1. Create new table with better partitioning
CREATE OR REPLACE TABLE sales_new 
PARTITIONED BY (region)  -- New partition scheme based on query analysis
AS SELECT * FROM sales;

-- 2. Atomic swap (rename)
ALTER TABLE sales RENAME TO sales_old_backup;
ALTER TABLE sales_new RENAME TO sales;

-- 3. Verify and drop old table
DROP TABLE sales_old_backup;
```

**Pros:**
- ✅ Atomic table swap (minimal downtime)
- ✅ Preserves Delta history in new table
- ✅ Can OPTIMIZE ZORDER during copy

**Cons:**
- ❌ Doubles storage temporarily
- ❌ Requires full table rewrite

---

### Option 2: SHALLOW CLONE with New Partitioning

**Best for:** Testing partition strategies

```sql
-- Create a clone to test new partitioning
CREATE TABLE sales_test_region_partition
SHALLOW CLONE sales
PARTITIONED BY (region);

-- Test query performance
SELECT * FROM sales_test_region_partition WHERE region = 'EMEA';

-- If better, promote to production (see Option 1)
```

**Pros:**
- ✅ Fast (metadata-only operation)
- ✅ No data duplication initially
- ✅ Test before committing

**Cons:**
- ❌ Not production-ready (needs full copy for production)

---

### Option 3: OPTIMIZE ZORDER BY (Clustering, not Partitioning)

**Best for:** When you CAN'T repartition but want better performance

```sql
-- Instead of repartitioning, cluster data within partitions
OPTIMIZE sales
ZORDER BY (region, status, created_date);
```

**How it works:**
- Doesn't change partition structure
- Co-locates related rows within partitions
- Improves filter performance without repartitioning

**Pros:**
- ✅ Works on existing tables (no recreation)
- ✅ Can help even with suboptimal partitioning
- ✅ Incremental (can run regularly)

**Cons:**
- ❌ Not as effective as true repartitioning
- ❌ Requires regular maintenance

---

### Option 4: Incremental Repartitioning (Future Writes Only)

**Best for:** Time-series data where old data is rarely queried

```sql
-- Stop writing to old table
-- Start writing to new table with better partitioning
CREATE TABLE sales_v2
PARTITIONED BY (region)
AS SELECT * FROM sales WHERE created_date >= '2026-01-01';

-- Union view for seamless queries
CREATE VIEW sales_unified AS
SELECT * FROM sales WHERE created_date < '2026-01-01'
UNION ALL
SELECT * FROM sales_v2;
```

**Pros:**
- ✅ No downtime
- ✅ Old data stays untouched
- ✅ New data benefits immediately

**Cons:**
- ❌ Queries span two tables (slower)
- ❌ Eventually need full migration

---

## Data Kiln Works Intelligent Repartitioning Recommendation

### Step 1: Analyze Query Patterns

```python
# Data Kiln Works already does this!
# It analyzes query_history to find frequently filtered columns
```

### Step 2: Compare Current vs Optimal Partitioning

```sql
-- Query to analyze what partitioning SHOULD be:
SELECT 
    table_name,
    current_partition_columns,
    recommended_partition_columns,
    query_count,
    avg_rows_scanned,
    potential_speedup
FROM partition_analysis_view;
```

**Example output:**
```
table_name: sales
current_partition: created_date
recommended_partition: region, status
query_count: 1,247 queries filter by region
potential_speedup: 15x (scans 1 partition instead of 15)
```

### Step 3: Automated Repartitioning Workflow

```python
# Future Data Kiln Works feature:
POST /api/tables/sales/analyze-partitions
→ Returns recommendation

POST /api/tables/sales/repartition
{
  "new_partitions": ["region", "status"],
  "strategy": "ctas_atomic_swap",
  "optimize_zorder": ["created_date"],
  "backup": true
}
→ Executes CTAS, swap, backup, cleanup
```

---

## Decision Matrix

| Scenario | Recommended Option | Timeline |
|----------|-------------------|----------|
| **Fresh table, wrong partitions** | CTAS (Option 1) | Immediate |
| **Testing partition strategy** | SHALLOW CLONE (Option 2) | 1 day |
| **Can't repartition (prod locked)** | ZORDER (Option 3) | Hourly/Daily |
| **Time-series, old data static** | Incremental (Option 4) | Ongoing |
| **High query volume, 10x+ speedup** | CTAS (Option 1) | Plan outage |

---

## Best Practices

### Before Repartitioning:
1. ✅ **Analyze query patterns** (Data Kiln Works does this automatically)
2. ✅ **Estimate storage requirements** (2x during CTAS)
3. ✅ **Test with SHALLOW CLONE first**
4. ✅ **Benchmark query performance** (before/after)
5. ✅ **Plan downtime window** (or use atomic swap)

### After Repartitioning:
1. ✅ **OPTIMIZE ZORDER BY** frequently queried columns
2. ✅ **ANALYZE TABLE** to update statistics
3. ✅ **Monitor query performance** (validate improvement)
4. ✅ **Update data pipelines** (partition column in writes)

---

## Future Data Kiln Works Features

### 1. Partition Health Dashboard
- Shows tables with suboptimal partitioning
- Highlights query pattern mismatches
- Estimates potential speedup

### 2. One-Click Repartitioning
- Automated CTAS workflow
- Progress monitoring
- Automatic rollback on failure

### 3. Adaptive Partitioning
- ML-based partition recommendation
- Learns from query patterns over time
- Suggests seasonal repartitioning (e.g., by fiscal_quarter)

---

## Summary

**Q: Can I change partitioning inline?**  
A: No, but you can recreate the table with CTAS and atomic swap.

**Q: How do I know if I need to repartition?**  
A: Data Kiln Works analyzes query patterns - if your top filtered columns don't match partition columns, you need to repartition.

**Q: What if I can't take downtime?**  
A: Use ZORDER BY for incremental improvements, or incremental repartitioning for new data.

**Q: Is repartitioning worth it?**  
A: If 80%+ of queries filter by a non-partition column, repartitioning can give 10-100x speedup.
