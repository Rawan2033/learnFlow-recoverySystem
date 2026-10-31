"""Local-only launcher; a paid run still requires an explicit UI/API action."""
import uvicorn


if __name__ == '__main__':
    uvicorn.run('learnflow.web:local_app', factory=True, host='127.0.0.1', port=8000, workers=1)
