Set-Location 'E:\ABU\01_PRODUCTS\360Car-LotVue\app'
$log = 'E:\ABU\01_PRODUCTS\360Car-LotVue\rnd\_gate_probe\demo5.log'
"START $(Get-Date)" | Set-Content $log
python studio_spin.py --vin 1HGCV1F31MA037775 --bg studio_white   2>&1 | Add-Content $log
python studio_spin.py --vin 4S4BSENC1K3205956 --bg studio_dark    2>&1 | Add-Content $log
python studio_spin.py --vin WDDEJ7EB9CA029569 --bg studio_graphite 2>&1 | Add-Content $log
"DONE $(Get-Date)" | Add-Content $log
