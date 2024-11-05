APP      := tremc
BUILDDIR := build
PREFIX   ?= $(HOME)/.local

$(APP): $(APP).py
	mkdir -p "$(BUILDDIR)"
	cp $< "$(BUILDDIR)/__main__.py"
	python3 -m pip install --target="$(BUILDDIR)" --upgrade 'geoip2>=4.8.0,<5.0.0' 'pyperclip>=1.9.0,<2.0.0'
	python3 -m zipapp "$(BUILDDIR)" --output=$@ --python='/usr/bin/env python3'

.PHONY: clean
clean:
	-rm -r $(APP) "$(BUILDDIR)"

.PHONY: install
install: $(APP)
	install -D -m 755 $< "$(PREFIX)/bin/tremc"
	install -D -m 644 tremc.1 "$(PREFIX)/share/man/man1/tremc.1"
	install -D -m 644 "completion/bash/tremc.sh" "$(PREFIX)/share/bash-completion/completions/tremc"
	install -D -m 644 "completion/zsh/_tremc" "$(PREFIX)/share/zsh/site-functions/_tremc"

