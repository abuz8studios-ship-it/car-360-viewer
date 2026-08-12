import sqlite3
c = sqlite3.connect(r'E:\ABU\01_PRODUCTS\360Car-LotVue\app\vehicles.db')
n = c.execute("DELETE FROM vehicles WHERE vin LIKE 'LVTESTGATE%'").rowcount
c.commit()
print('test vehicle rows deleted:', n)
print('hotspots left for WDDEJ7EB9CA029569:',
      c.execute("SELECT COUNT(*) FROM hotspots WHERE vin='WDDEJ7EB9CA029569'").fetchone()[0])
