# OneLake Integration - Quick Start

## ✅ **IMPLEMENTED - Ready to Use!**

The core backend API for OneLake catalog mounting is now fully implemented and running.

---

## 🚀 **How to Mount Your OneLake Lakehouse**

### **Step 1: Get Your Azure Credentials**

You mentioned you have:
- `TENANT_ID` - Azure AD tenant ID
- `CLIENT_ID` - Service principal client ID  
- `CLIENT_SECRET` - Service principal secret

### **Step 2: Mount OneLake Catalog via API**

```bash
curl -X POST http://localhost:8891/api/catalogs/onelake/mount \
  -H "Content-Type: application/json" \
  -d '{
    "workspace": "your-workspace-name",
    "lakehouse": "your-lakehouse-name",
    "tenant_id": "YOUR_TENANT_ID",
    "client_id": "YOUR_CLIENT_ID",
    "client_secret": "YOUR_CLIENT_SECRET"
  }'
```

**Response:**
```json
{
  "success": true,
  "catalog_id": "onelake_your-lakehouse-name",
  "workspace": "your-workspace-name",
  "lakehouse": "your-lakehouse-name",
  "type": "onelake",
  "read_only": true,
  "table_count": 5,
  "tables": [
    {
      "name": "orders",
      "catalog": "onelake_your-lakehouse-name",
      "source": "onelake",
      "read_only": true
    }
  ],
  "message": "Successfully mounted OneLake catalog 'onelake_your-lakehouse-name' with 5 tables"
}
```

---

## 📊 **Query Your OneLake Tables**

### **List All Mounted Catalogs**

```bash
curl http://localhost:8891/api/catalogs/onelake
```

### **List Tables in a Catalog**

```bash
curl http://localhost:8891/api/catalogs/onelake/onelake_your-lakehouse-name/tables
```

### **Get Table Metadata**

```bash
curl http://localhost:8891/api/catalogs/onelake/onelake_your-lakehouse-name/tables/orders
```

**Response:**
```json
{
  "name": "orders",
  "catalog": "onelake_your-lakehouse-name",
  "source": "onelake",
  "read_only": true,
  "delta_version": 42,
  "num_files": 128,
  "columns": [
    {"name": "order_id", "type": "int64", "nullable": false},
    {"name": "customer_id", "type": "int64", "nullable": true},
    {"name": "order_date", "type": "timestamp[ms]", "nullable": true},
    {"name": "total_amount", "type": "double", "nullable": true}
  ],
  "num_columns": 4,
  "location": "abfss://workspace@onelake.dfs.fabric.microsoft.com/lakehouse/Files/Tables/orders",
  "workspace": "your-workspace-name",
  "lakehouse": "your-lakehouse-name"
}
```

### **Query Table Data**

```bash
curl -X POST http://localhost:8891/api/catalogs/onelake/onelake_your-lakehouse-name/query \
  -H "Content-Type: application/json" \
  -d '{
    "table_name": "orders",
    "limit": 100
  }'
```

### **Query with Filters**

```bash
curl -X POST http://localhost:8891/api/catalogs/onelake/onelake_your-lakehouse-name/query \
  -H "Content-Type: application/json" \
  -d '{
    "table_name": "orders",
    "limit": 100,
    "filters": [
      ["order_date", ">=", "2024-01-01"]
    ]
  }'
```

### **Query with Custom SQL**

```bash
curl -X POST http://localhost:8891/api/catalogs/onelake/onelake_your-lakehouse-name/query \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "SELECT DATE_TRUNC('\''month'\'', order_date) as month, COUNT(*) as count FROM orders GROUP BY month ORDER BY month DESC LIMIT 12"
  }'
```

---

## 🧪 **Test Connection**

```bash
curl -X POST http://localhost:8891/api/catalogs/onelake/onelake_your-lakehouse-name/test
```

**Response:**
```json
{
  "catalog_id": "onelake_your-lakehouse-name",
  "connected": true,
  "message": "Connection successful"
}
```

---

## 🔓 **Unmount Catalog**

```bash
curl -X DELETE http://localhost:8891/api/catalogs/onelake/onelake_your-lakehouse-name
```

---

## 🎨 **Next: UI Integration**

The backend is ready! Now we can add UI elements to:

1. **"Mount External Storage" Form** in Platform Settings
   - Input fields for workspace, lakehouse, credentials
   - Test connection button
   - Mount button

2. **Catalog Explorer** - Show OneLake catalogs with cloud badge
   ```
   📁 Catalogs
     📂 local_warehouse (local)
     ☁️ onelake_sales (external, read-only)
   ```

3. **SQL Editor** - OneLake tables available for queries
   ```sql
   SELECT * FROM onelake_sales.orders
   WHERE order_date >= '2024-01-01'
   ```

4. **Data Lineage** - Show OneLake tables in lineage graph

---

## 🔐 **Security Notes**

- ✅ **Admin-only mounting** - Only administrators can mount/unmount catalogs
- ✅ **Read-only enforcement** - No writes to OneLake (prevents accidental data modification)
- ✅ **Credential encryption** - Credentials stored securely
- ✅ **Bearer token auth** - Azure AD service principal authentication

---

## ✨ **Example: Real-World Usage**

```python
# 1. Mount your production OneLake lakehouse
import requests

response = requests.post('http://localhost:8891/api/catalogs/onelake/mount', json={
    'workspace': 'analytics-prod',
    'lakehouse': 'sales-data',
    'tenant_id': os.getenv('AZURE_TENANT_ID'),
    'client_id': os.getenv('AZURE_CLIENT_ID'),
    'client_secret': os.getenv('AZURE_CLIENT_SECRET')
})

catalog = response.json()
print(f"Mounted {catalog['table_count']} tables from OneLake")

# 2. Query orders from OneLake + join with local customer data
query = """
SELECT 
    o.order_id,
    o.order_date,
    o.total_amount,
    c.customer_name,
    c.customer_segment
FROM onelake_sales-data.orders AS o
JOIN local_warehouse.dbo.customers AS c
    ON o.customer_id = c.id
WHERE o.order_date >= CURRENT_DATE - INTERVAL '30 days'
ORDER BY o.order_date DESC
LIMIT 100
"""

# Execute via SQL Editor or API
response = requests.post(
    'http://localhost:8891/api/catalogs/onelake/onelake_sales-data/query',
    json={'sql': query}
)

results = response.json()
print(f"Found {results['rows']} recent orders")
```

---

## 🎯 **Status: READY TO USE**

✅ Backend API implemented  
✅ Azure authentication configured  
✅ Delta Lake support enabled  
✅ DuckDB integration complete  
✅ Read-only enforcement active  
⏳ UI integration (next step)

**You can now mount your OneLake lakehouses via the API!** 🚀

---

## 📝 **What to Provide**

To mount your OneLake lakehouse, you need:

1. **Workspace Name** - Your Fabric workspace (e.g., `analytics-prod`)
2. **Lakehouse Name** - The lakehouse to mount (e.g., `sales-data`)
3. **Credentials** - Already have:
   - `TENANT_ID`
   - `CLIENT_ID`
   - `CLIENT_SECRET`

**Ready to test?** Just provide your workspace and lakehouse names, and I'll help you mount them!
