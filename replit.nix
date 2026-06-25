{ pkgs }: {
  deps = [
    pkgs.python3Full
    pkgs.chromium
    pkgs.chromedriver
  ];
  env = {
    CHROME_BIN = "${pkgs.chromium}/bin/chromium";
    CHROMEDRIVER_PATH = "${pkgs.chromedriver}/bin/chromedriver";
  };
}
