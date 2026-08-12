import sqlite3
c = sqlite3.connect(r'E:\ABU\01_PRODUCTS\360Car-LotVue\app\vehicles.db')
print('vehicles del:', c.execute("DELETE FROM vehicles WHERE vin IN ('LV36077EFB5465FBF','TESTGATE3D')").rowcount)
print('users del:', c.execute("DELETE FROM users WHERE email IN ('gatetest@lotvue.test','dealer60@lotvue.test')").rowcount)
c.commit()
print('remaining test rows:', c.execute("SELECT COUNT(*) FROM vehicles WHERE vin LIKE 'LV360%' OR vin LIKE 'TESTGATE%'").fetchone()[0])
