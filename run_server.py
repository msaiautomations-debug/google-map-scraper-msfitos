import app.config as config
import app.views  # noqa: F401


if __name__ == "__main__":
    config.app.run(host="127.0.0.1", port=5000)
