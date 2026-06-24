{
  lib,
  stdenv,
  python3,
  chromium,
  makeWrapper,
}:

let
  python = python3.withPackages (ps: with ps; [
    selenium
    requests
  ]);
  src = lib.cleanSourceWith {
    src = lib.cleanSource ../.;
    filter = path: _type:
      let name = builtins.baseNameOf path;
      in name == "visa.py" || name == "embassy.py";
  };
in
stdenv.mkDerivation {
  pname = "us-visa-scheduler";
  version = "unstable";
  inherit src;

  nativeBuildInputs = [ makeWrapper ];
  buildInputs = [ python chromium ];

  installPhase = ''
    mkdir -p $out/bin $out/share/us-visa-scheduler
    cp "$src/visa.py" "$src/embassy.py" $out/share/us-visa-scheduler/
    makeWrapper ${python}/bin/python $out/bin/us-visa-scheduler \
      --add-flags $out/share/us-visa-scheduler/visa.py \
      --prefix PATH : ${lib.makeBinPath [ chromium ]}
  '';

  meta.mainProgram = "us-visa-scheduler";
}
