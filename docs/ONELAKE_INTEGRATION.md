# OneLake Lakehouse Integration Guide

## Mount Microsoft Fabric OneLake as Read-Only Catalog in Data Kiln Works

---

## 🎯 Overview

Microsoft OneLake (Fabric Lakehouse) stores data in **Delta Lake format** on Azure Data Lake Storage Gen2 (ADLS Gen2). Since Data Kiln Works natively supports Delta Lake via DuckDB, we can mount OneLake as a read-only external catalog.

---

## 📋 Prerequisites

### 1. OneLake Access Requirements

- **Microsoft Fabric Workspace** with a Lakehouse
- **Authentication Method** (choose one):
  - Azure AD Service Principal (recommended for production)
  - SAS Token (simpler for development)
  - Azure Storage Account Key (less secure)
  - Managed Identity (for Azure-hosted deployments)

### 2. OneLake URL Structure

```
https://onelake.dfs.fabric.microsoft.com/{workspace}/{lakehouse}/Files/
```

**Example:**
```
https://onelake.dfs.fabric.microsoft.com/my-workspace/my-lakehouse/Files/Tables/sales/
```

---

## 🔧 Implementation Options

### **Option 1: DuckDB Azure Extension (Recommended)**

Use DuckDB's native Azure extension to read OneLake Delta tables.

#### **Step 1: Install DuckDB Azure Extension**

```python
# In your Data Kiln Works container
import duckdb

conn = duckdb.connect()

# Install and load Azure extension
conn.execute("INSTALL azure;")
conn.execute("LOAD azure;")
```

#### **Step 2: Configure Azure Authentication**

```python
# Method A: Using SAS Token
conn.execute("""
    CREATE SECRET onelake_secret (
        TYPE AZURE,
        CONNECTION_STRING 'DefaultEndpointsProtocol=https;AccountName=onelake;SharedAccessSignature=sp=r&st=2024-01-01...'
    );
""")

# Method B: Using Service Principal (Recommended)
conn.execute("""
    CREATE SECRET onelake_secret (
        TYPE AZURE,
        PROVIDER SERVICE_PRINCIPAL,
        TENANT_ID '<your-tenant-id>',
        CLIENT_ID '<your-client-id>',
        CLIENT_SECRET '<your-client-secret>',
        ACCOUNT_NAME 'onelake'
    );
""")

# Method C: Using Account Key
conn.execute("""
    CREATE SECRET onelake_secret (
        TYPE AZURE,
        CONNECTION_STRING 'DefaultEndpointsProtocol=https;AccountName=onelake;AccountKey=<key>==;EndpointSuffix=core.windows.net'
    );
""")
```

#### **Step 3: Read OneLake Delta Tables**

```python
# Read a specific Delta table
query = """
    SELECT * FROM delta_scan('az://workspace/lakehouse/Files/Tables/sales')
"""
result = conn.execute(query).fetchall()

# Read with filters (pushdown optimization)
query = """
    SELECT * FROM delta_scan('az://workspace/lakehouse/Files/Tables/sales')
    WHERE sale_date >= '2024-01-01'
"""
```

#### **Step 4: Create External Catalog in Data Kiln Works**

Add to `web/catalog.py`:

```python
def mount_onelake_catalog(workspace: str, lakehouse: str, credentials: dict) -> dict:
    """
    Mount OneLake lakehouse as read-only external catalog.
    
    Args:
        workspace: Fabric workspace name
        lakehouse: Lakehouse name
        credentials: Azure authentication credentials
    
    Returns:
        Catalog metadata
    """
    import duckdb
    
    conn = duckdb.connect()
    
    # Install Azure extension
    conn.execute("INSTALL azure;")
    conn.execute("LOAD azure;")
    
    # Configure authentication
    if credentials.get('type') == 'service_principal':
        conn.execute(f"""
            CREATE OR REPLACE SECRET onelake_{lakehouse} (
                TYPE AZURE,
                PROVIDER SERVICE_PRINCIPAL,
                TENANT_ID '{credentials['tenant_id']}',
                CLIENT_ID '{credentials['client_id']}',
                CLIENT_SECRET '{credentials['client_secret']}',
                ACCOUNT_NAME 'onelake'
            );
        """)
    elif credentials.get('type') == 'sas_token':
        conn.execute(f"""
            CREATE OR REPLACE SECRET onelake_{lakehouse} (
                TYPE AZURE,
                CONNECTION_STRING '{credentials['connection_string']}'
            );
        """)
    
    # Discover tables in OneLake lakehouse
    base_url = f"az://{workspace}/{lakehouse}/Files/Tables"
    
    # List available tables (this requires Azure Storage SDK)
    from azure.storage.filedatalake import DataLakeServiceClient
    
    service_client = DataLakeServiceClient(
        account_url=f"https://onelake.dfs.fabric.microsoft.com",
        credential=credentials.get('credential')
    )
    
    filesystem = service_client.get_file_system_client(f"{workspace}/{lakehouse}")
    paths = filesystem.get_paths(path="Files/Tables")
    
    tables = []
    for path in paths:
        if path.is_directory and not path.name.startswith('_'):
            table_name = path.name.split('/')[-1]
            tables.append({
                'name': table_name,
                'path': f"{base_url}/{table_name}",
                'type': 'delta',
                'read_only': True
            })
    
    return {
        'catalog_name': f'onelake_{lakehouse}',
        'workspace': workspace,
        'lakehouse': lakehouse,
        'type': 'external',
        'storage': 'azure_onelake',
        'read_only': True,
        'tables': tables
    }
```

---

### **Option 2: Delta Lake Python Reader**

Use `deltalake` Python library to read OneLake tables.

#### **Step 1: Install Dependencies**

```dockerfile
# Add to Dockerfile
RUN pip install deltalake azure-identity azure-storage-file-datalake
```

#### **Step 2: Create OneLake Reader**

```python
# web/onelake_reader.py
from deltalake import DeltaTable
from azure.identity import DefaultAzureCredential, ClientSecretCredential
import pandas as pd

class OneLakeReader:
    """Read-only access to Microsoft OneLake Delta tables."""
    
    def __init__(self, workspace: str, lakehouse: str, credentials: dict):
        self.workspace = workspace
        self.lakehouse = lakehouse
        self.base_url = f"abfss://{workspace}@onelake.dfs.fabric.microsoft.com/{lakehouse}/Files/Tables"
        
        # Configure Azure credential
        if credentials.get('type') == 'service_principal':
            self.credential = ClientSecretCredential(
                tenant_id=credentials['tenant_id'],
                client_id=credentials['client_id'],
                client_secret=credentials['client_secret']
            )
        else:
            self.credential = DefaultAzureCredential()
    
    def read_table(self, table_name: str, filters=None) -> pd.DataFrame:
        """Read OneLake Delta table as pandas DataFrame."""
        table_path = f"{self.base_url}/{table_name}"
        
        storage_options = {
            'bearer_token': self.credential.get_token("https://storage.azure.com/.default").token,
            'use_fabric_endpoint': True
        }
        
        dt = DeltaTable(table_path, storage_options=storage_options)
        
        if filters:
            return dt.to_pandas(filters=filters)
        else:
            return dt.to_pandas()
    
    def list_tables(self) -> list:
        """List all tables in OneLake lakehouse."""
        from azure.storage.filedatalake import DataLakeServiceClient
        
        service_client = DataLakeServiceClient(
            account_url="https://onelake.dfs.fabric.microsoft.com",
            credential=self.credential
        )
        
        filesystem = service_client.get_file_system_client(
            f"{self.workspace}/{self.lakehouse}"
        )
        
        tables = []
        paths = filesystem.get_paths(path="Files/Tables")
        
        for path in paths:
            if path.is_directory and not path.name.startswith('_'):
                table_name = path.name.split('/')[-1]
                tables.append(table_name)
        
        return tables
    
    def get_table_metadata(self, table_name: str) -> dict:
        """Get metadata for a specific table."""
        table_path = f"{self.base_url}/{table_name}"
        
        storage_options = {
            'bearer_token': self.credential.get_token("https://storage.azure.com/.default").token,
            'use_fabric_endpoint': True
        }
        
        dt = DeltaTable(table_path, storage_options=storage_options)
        
        return {
            'name': table_name,
            'schema': dt.schema().to_pyarrow().to_string(),
            'version': dt.version(),
            'num_files': len(dt.files()),
            'metadata': dt.metadata()
        }
```

#### **Step 3: Integrate with Data Kiln Works API**

```python
# Add to web/app.py
from web.onelake_reader import OneLakeReader

@app.post("/api/catalogs/onelake/mount")
async def mount_onelake_catalog(
    workspace: str,
    lakehouse: str,
    credentials: dict,
    catalog_name: Optional[str] = None
):
    """Mount OneLake lakehouse as read-only catalog."""
    try:
        reader = OneLakeReader(workspace, lakehouse, credentials)
        
        # List tables
        tables = reader.list_tables()
        
        # Get metadata for each table
        catalog_tables = []
        for table in tables:
            metadata = reader.get_table_metadata(table)
            catalog_tables.append({
                'name': table,
                'type': 'delta',
                'read_only': True,
                'source': 'onelake',
                'metadata': metadata
            })
        
        # Store catalog configuration
        catalog_id = catalog_name or f"onelake_{lakehouse}"
        
        ONELAKE_CATALOGS[catalog_id] = {
            'workspace': workspace,
            'lakehouse': lakehouse,
            'credentials': credentials,  # Store securely!
            'tables': catalog_tables,
            'reader': reader
        }
        
        return {
            'catalog_id': catalog_id,
            'workspace': workspace,
            'lakehouse': lakehouse,
            'table_count': len(catalog_tables),
            'tables': catalog_tables,
            'status': 'mounted',
            'read_only': True
        }
        
    except Exception as e:
        logger.error(f"Failed to mount OneLake catalog: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/catalogs/onelake/{catalog_id}/tables")
async def list_onelake_tables(catalog_id: str):
    """List tables in mounted OneLake catalog."""
    if catalog_id not in ONELAKE_CATALOGS:
        raise HTTPException(status_code=404, detail="Catalog not found")
    
    catalog = ONELAKE_CATALOGS[catalog_id]
    return {
        'catalog_id': catalog_id,
        'tables': catalog['tables']
    }

@app.post("/api/catalogs/onelake/{catalog_id}/query")
async def query_onelake_table(
    catalog_id: str,
    table_name: str,
    query: Optional[str] = None,
    filters: Optional[dict] = None
):
    """Query table from OneLake catalog."""
    if catalog_id not in ONELAKE_CATALOGS:
        raise HTTPException(status_code=404, detail="Catalog not found")
    
    catalog = ONELAKE_CATALOGS[catalog_id]
    reader = catalog['reader']
    
    try:
        # Read table with optional filters
        df = reader.read_table(table_name, filters=filters)
        
        # If custom query provided, use DuckDB
        if query:
            import duckdb
            result = duckdb.query(query.replace(table_name, 'df')).df()
            df = result
        
        return {
            'table': table_name,
            'rows': len(df),
            'columns': df.columns.tolist(),
            'data': df.head(100).to_dict(orient='records')
        }
        
    except Exception as e:
        logger.error(f"Failed to query OneLake table: {e}")
        raise HTTPException(status_code=500, detail=str(e))
```

---

### **Option 3: ADLS Gen2 Mount Point**

Mount OneLake as an ADLS Gen2 filesystem.

#### **Step 1: Configure Azure Storage Mount**

```python
# web/storage_mounts.py
from azure.storage.filedatalake import DataLakeServiceClient
from azure.identity import ClientSecretCredential

def mount_onelake_as_adls(
    workspace: str,
    lakehouse: str,
    tenant_id: str,
    client_id: str,
    client_secret: str
) -> str:
    """
    Mount OneLake lakehouse using ADLS Gen2 protocol.
    Returns local mount path.
    """
    
    # Create credential
    credential = ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret
    )
    
    # Create service client
    service_client = DataLakeServiceClient(
        account_url="https://onelake.dfs.fabric.microsoft.com",
        credential=credential
    )
    
    # Get filesystem (workspace/lakehouse)
    filesystem = service_client.get_file_system_client(
        f"{workspace}/{lakehouse}"
    )
    
    # Download Tables directory to local cache
    local_path = f"/tmp/onelake_cache/{workspace}/{lakehouse}"
    os.makedirs(local_path, exist_ok=True)
    
    # Sync tables (read-only)
    sync_onelake_to_local(filesystem, "Files/Tables", local_path)
    
    return local_path

def sync_onelake_to_local(filesystem, remote_path: str, local_path: str):
    """Sync OneLake tables to local cache."""
    paths = filesystem.get_paths(path=remote_path, recursive=True)
    
    for path in paths:
        local_file = os.path.join(local_path, path.name.replace(f"{remote_path}/", ""))
        
        if not path.is_directory:
            # Download file
            os.makedirs(os.path.dirname(local_file), exist_ok=True)
            file_client = filesystem.get_file_client(path.name)
            
            with open(local_file, 'wb') as f:
                download = file_client.download_file()
                download.readinto(f)
```

---

## 🎨 UI Integration

### **Add OneLake Catalog to Catalog Explorer**

```javascript
// In web/templates/index.html - Catalog Explorer

// Add OneLake catalog type
const onelakeCatalogs = [
    {
        id: 'onelake_sales',
        name: 'OneLake Sales',
        workspace: 'analytics-workspace',
        lakehouse: 'sales-lakehouse',
        type: 'external',
        icon: '☁️',
        read_only: true,
        tables: ['orders', 'customers', 'products']
    }
];

// Render OneLake catalogs in UI
function renderOneLakeCatalogs() {
    return onelakeCatalogs.map(catalog => `
        <div class="catalog-item onelake-catalog">
            <div class="catalog-header">
                <span class="catalog-icon">${catalog.icon}</span>
                <span class="catalog-name">${catalog.name}</span>
                <span class="badge read-only">READ-ONLY</span>
                <span class="badge cloud">☁️ OneLake</span>
            </div>
            <div class="catalog-info">
                <span class="info-item">
                    <i class="ph ph-buildings"></i> ${catalog.workspace}
                </span>
                <span class="info-item">
                    <i class="ph ph-database"></i> ${catalog.lakehouse}
                </span>
            </div>
            <div class="catalog-tables">
                ${catalog.tables.map(t => `
                    <div class="table-item">
                        <i class="ph ph-table"></i>
                        <span>${t}</span>
                        <span class="badge delta">Δ</span>
                    </div>
                `).join('')}
            </div>
        </div>
    `).join('');
}
```

---

## 🔐 Security Best Practices

### **1. Store Credentials Securely**

```python
# Use environment variables or secrets manager
import os
from cryptography.fernet import Fernet

class SecureCredentialStore:
    """Encrypt OneLake credentials at rest."""
    
    def __init__(self):
        # Load encryption key from environment
        self.key = os.getenv('CREDENTIAL_ENCRYPTION_KEY').encode()
        self.cipher = Fernet(self.key)
    
    def encrypt_credentials(self, credentials: dict) -> str:
        """Encrypt credentials before storing."""
        import json
        plaintext = json.dumps(credentials).encode()
        return self.cipher.encrypt(plaintext).decode()
    
    def decrypt_credentials(self, encrypted: str) -> dict:
        """Decrypt stored credentials."""
        import json
        decrypted = self.cipher.decrypt(encrypted.encode())
        return json.loads(decrypted.decode())
```

### **2. Use Service Principal (Production)**

```bash
# Create Azure AD App Registration
az ad app create --display-name "DataKilnWorks-OneLake"

# Create Service Principal
az ad sp create --id <app-id>

# Assign OneLake read permissions
az role assignment create \
    --assignee <sp-object-id> \
    --role "Storage Blob Data Reader" \
    --scope "/subscriptions/<sub-id>/resourceGroups/<rg>/providers/Microsoft.Storage/storageAccounts/onelake"
```

### **3. Implement Read-Only Enforcement**

```python
# Ensure no writes to OneLake
class ReadOnlyOneLakeCatalog:
    """Enforce read-only access to OneLake."""
    
    def query(self, sql: str):
        # Block any write operations
        write_keywords = ['INSERT', 'UPDATE', 'DELETE', 'DROP', 'CREATE', 'ALTER']
        if any(kw in sql.upper() for kw in write_keywords):
            raise PermissionError("OneLake catalogs are read-only")
        
        # Execute read query
        return self._execute_read_query(sql)
```

---

## 📊 Example: Complete Integration

```python
# Complete example of mounting OneLake

from web.onelake_reader import OneLakeReader

# 1. Configure credentials
credentials = {
    'type': 'service_principal',
    'tenant_id': '<your-tenant-id>',
    'client_id': '<your-client-id>',
    'client_secret': '<your-client-secret>'
}

# 2. Mount OneLake lakehouse
reader = OneLakeReader(
    workspace='analytics-prod',
    lakehouse='sales-data',
    credentials=credentials
)

# 3. List available tables
tables = reader.list_tables()
print(f"Found {len(tables)} tables: {tables}")

# 4. Query a table
df = reader.read_table('orders', filters=[
    ('order_date', '>=', '2024-01-01')
])

print(f"Loaded {len(df)} orders")

# 5. Use in SQL Editor
import duckdb
conn = duckdb.connect()

# Register DataFrame as DuckDB view
conn.register('onelake_orders', df)

# Query with DuckDB SQL
result = conn.execute("""
    SELECT 
        DATE_TRUNC('month', order_date) as month,
        COUNT(*) as order_count,
        SUM(total_amount) as revenue
    FROM onelake_orders
    GROUP BY month
    ORDER BY month DESC
""").df()

print(result)
```

---

## 🚀 Next Steps

1. **Implement Option 1** (DuckDB Azure Extension) - simplest and most performant
2. **Add UI for OneLake catalogs** - catalog explorer with cloud badge
3. **Cache OneLake metadata** - avoid repeated API calls
4. **Add refresh mechanism** - periodic sync of table metadata
5. **Monitor usage** - track queries to OneLake tables
6. **Cost tracking** - monitor Azure egress fees (if applicable)

---

## 📝 Configuration Example

```yaml
# config/onelake_catalogs.yaml
catalogs:
  - name: onelake_sales
    workspace: analytics-prod
    lakehouse: sales-data
    auth:
      type: service_principal
      tenant_id: ${AZURE_TENANT_ID}
      client_id: ${AZURE_CLIENT_ID}
      client_secret: ${AZURE_CLIENT_SECRET}
    read_only: true
    cache_ttl: 300  # 5 minutes
    
  - name: onelake_marketing
    workspace: marketing-workspace
    lakehouse: campaign-data
    auth:
      type: sas_token
      connection_string: ${ONELAKE_SAS_TOKEN}
    read_only: true
    cache_ttl: 600
```

---

## ✅ Benefits

- ✅ **Zero data duplication** - query OneLake directly
- ✅ **Read-only safety** - no accidental writes
- ✅ **Delta Lake native** - full time travel and schema evolution
- ✅ **Cost-effective** - minimal egress (filter pushdown)
- ✅ **Unified view** - OneLake + local catalogs in one UI
- ✅ **Real-time data** - always query latest OneLake version

---

**Implementation Priority: HIGH**  
**Estimated Effort: 2-3 days**  
**Complexity: Medium**
