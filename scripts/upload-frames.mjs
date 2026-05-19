import fs from 'fs/promises';
import path from 'path';
import { put } from '@vercel/blob';

async function uploadFrames() {
  const folderPath = process.argv[2];

  if (!folderPath) {
    console.error('Please provide a folder path as a command line argument.');
    console.error('Usage: node scripts/upload-frames.mjs <path-to-folder>');
    process.exit(1);
  }

  try {
    const absoluteFolderPath = path.resolve(folderPath);
    const files = await fs.readdir(absoluteFolderPath);
    
    // Filter for jpg/jpeg files and sort them naturally (e.g. 1.jpg, 2.jpg, 10.jpg)
    const jpgFiles = files
      .filter(file => file.toLowerCase().endsWith('.jpg') || file.toLowerCase().endsWith('.jpeg'))
      .sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }));

    if (jpgFiles.length === 0) {
      console.log('No JPG files found in the specified directory.');
      return;
    }

    console.log(`Found ${jpgFiles.length} JPG files. Starting upload...`);

    let baseUrl = '';

    for (let i = 0; i < jpgFiles.length; i++) {
      const fileName = jpgFiles[i];
      const filePath = path.join(absoluteFolderPath, fileName);
      const fileBuffer = await fs.readFile(filePath);

      console.log(`Uploading frame ${i + 1}/${jpgFiles.length}...`);
      
      const blob = await put(`frames/${fileName}`, fileBuffer, {
        access: 'public',
      });

      // Capture the base URL from the first uploaded file
      if (!baseUrl) {
          const lastSlashIndex = blob.url.lastIndexOf('/');
          baseUrl = blob.url.substring(0, lastSlashIndex + 1);
      }
    }

    console.log('\nUpload complete!');
    console.log(`Successfully uploaded ${jpgFiles.length} frames.`);
    if (baseUrl) {
      console.log(`Base URL: ${baseUrl}`);
    }

  } catch (error) {
    console.error('An error occurred during the upload process:', error.message);
    process.exit(1);
  }
}

uploadFrames();
