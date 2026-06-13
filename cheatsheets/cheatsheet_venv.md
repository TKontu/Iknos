🔹 Create & Activate Virtual Environment

# Create a virtual environment in folder ".venv"

python -m venv .venv

# Activate (Linux/macOS)

source .venv/bin/activate

# Activate (Windows PowerShell)

.venv\Scripts\Activate.ps1

# Activate (Windows CMD)

.venv\Scripts\activate.bat

🔹 Deactivate
deactivate

🔹 Install & Manage Packages

# Upgrade pip

python -m pip install --upgrade pip

# Install a package

pip install requests

# Install multiple packages

pip install requests flask numpy

# Save installed packages to requirements.txt

pip freeze > requirements.txt

# Install from requirements.txt

pip install -r requirements.txt

# Show installed packages

pip list

🔹 Remove / Reset Environment

# Deactivate first

deactivate

# Remove environment

rm -rf .venv # Linux/macOS
rmdir /s .venv # Windows
