.NOTPARALLEL:

all:
q: quality

# to be launched as root
INSTALL_DEST = /utils
install:
	cp sleep-when-idle.py $(INSTALL_DEST)
	sed -i "s/VERSION:.*/VERSION: $(shell git describe --tags --always --dirty)/" $(INSTALL_DEST)/sleep-when-idle.py

quality:
	-pylint --rcfile /etc/pylint.rc *.py

.PHONY: all install q quality