pip freeze> reqtemp.txt
pip uninstall reqtemp.txt -y
rm reqtemp.txt
pip install -r requirements.txt
pip freeze> requirements.txt
