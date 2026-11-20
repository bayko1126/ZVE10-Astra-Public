import os
from pathlib import Path
from elftools.elf.elffile import ELFFile

ROOT = Path(__file__).resolve().parents[2]
src_path = ROOT / "firmware" / "gen4" / "lib" / "libObj.so"
out_path = ROOT / "build_out" / "libObj.so"
out_path.parent.mkdir(parents=True, exist_ok=True)

with open(src_path, 'rb') as f:
    data = bytearray(f.read())

with open(src_path, 'rb') as f:
    elffile = ELFFile(f)
    dynstr = elffile.get_section_by_name('.dynstr')
    dynstr_offset = dynstr.header['sh_offset']
    dynstr_data = dynstr.data()

# Find Application_System_Init in dynstr data
target = b'Application_System_Init'
idx = dynstr_data.find(target)
if idx == -1:
    raise RuntimeError('Application_System_Init not found in .dynstr')

# Replacement: 22 bytes + NUL -> 'Application_G4Old_Init\0'
# Length of 'Application_G4Old_Init' is 22 characters + 1 NUL = 23 bytes.
# Wait, let's check exact lengths:
# 'Application_System_Init' -> 23 chars + NUL = 24 bytes? Let's check len(target)
print(f'Target len: {len(target)}, string: {target}')
replacement = b'Application_G4Old_Init\x00'
print(f'Replacement len: {len(replacement)}, string: {replacement}')
assert len(replacement) == len(target), f'Length mismatch: {len(replacement)} vs {len(target)}'

# Patch data
patch_offset = dynstr_offset + idx
data[patch_offset:patch_offset + len(target)] = replacement

with open(out_path, 'wb') as f:
    f.write(data)

print(f'Successfully generated {out_path} (size: {len(data)})')

# Verify with pyelftools
with open(out_path, 'rb') as f:
    ef2 = ELFFile(f)
    dynsym2 = ef2.get_section_by_name('.dynsym')
    found = False
    for sym in dynsym2.iter_symbols():
        if sym.name == 'Application_System_Init':
            found = True
            print('ERROR: Application_System_Init still found in dynsym!')
        elif 'Application_G4Old_Init' in sym.name:
            print(f'Found renamed symbol in dynsym: {sym.name}')

    if not found:
        print('VERIFIED: Application_System_Init is completely gone from dynsym!')
