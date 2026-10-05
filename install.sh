USER_SITE=$(python3 -c "import site; print(site.getusersitepackages())")
echo "Installing/updating Sawler..."
cp sawlr.py sawlr
chmod +x sawlr
sudo mkdir -p /usr/local/bin/ 
sudo mv sawlr /usr/local/bin/
echo "Installing/updating libsaw..."
mkdir -p "$USER_SITE"
cp libsaw.py "$USER_SITE/libsaw.py"
echo "Installation process finished. Try running 'sawlr -h' to confirm it works."
