# Formal verification of blocks with SymbiYosys (sby).

SPECS := spec_hex_encoder spec_warmboot spec_qspi \
         spec_flash_uid spec_json_key_parser spec_uf2 spec_flash

.PHONY: all $(SPECS) clean
all: $(SPECS)

# Run from the repo root so the `formal` and `blocks` packages import.
$(SPECS):
	cd .. && python -m formal.$@

clean:
	rm -rf build
